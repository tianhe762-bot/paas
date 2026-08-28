async def test_healthz(client):
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_inbound_requires_api_key(client):
    resp = await client.post(
        "/api/v1/message/inbound",
        json={"platform": "qq", "user_id": "u_a1", "message_id": "m0", "content": "午饭20"},
    )
    assert resp.status_code == 401


async def test_inbound_records_multi_items(client):
    headers = {"X-Api-Key": "test-api-key"}
    resp = await client.post(
        "/api/v1/message/inbound",
        headers=headers,
        json={
            "platform": "qq",
            "user_id": "u_api1",
            "chat_id": "c_api1",
            "message_id": "m1",
            "content": "今天微信中午和同事吃火锅花了86，微信打车回家23块",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert data["parsed_count"] == 2
    assert "已记录 2 笔" in data["reply_content"]


async def test_dedup_same_message_id(client):
    headers = {"X-Api-Key": "test-api-key"}
    body = {"platform": "qq", "user_id": "u_dedup", "chat_id": "c_dedup", "message_id": "md", "content": "今天微信奶茶15"}
    r1 = await client.post("/api/v1/message/inbound", headers=headers, json=body)
    r2 = await client.post("/api/v1/message/inbound", headers=headers, json=body)
    assert r1.json()["status"] == "success"
    assert "已记录过" in r2.json()["reply_content"]


async def test_debounce_confirm_flow(client):
    headers = {"X-Api-Key": "test-api-key"}
    base = {"platform": "qq", "user_id": "u_db", "chat_id": "c_db"}
    r1 = await client.post(
        "/api/v1/message/inbound", headers=headers,
        json={**base, "message_id": "db1", "content": "今天微信打车回家23块"},
    )
    assert r1.json()["status"] == "success"
    r2 = await client.post(
        "/api/v1/message/inbound", headers=headers,
        json={**base, "message_id": "db2", "content": "今天微信打车回家23块"},
    )
    data2 = r2.json()
    assert data2["status"] == "pending_confirmation"
    assert data2["requires_confirmation"] is True
    r3 = await client.post(
        "/api/v1/message/inbound", headers=headers,
        json={**base, "message_id": "db3", "content": "是"},
    )
    assert r3.json()["status"] == "success"
    assert "已记录 1 笔" in r3.json()["reply_content"]


async def test_ask_account_then_time_flow(client):
    headers = {"X-Api-Key": "test-api-key"}
    base = {"platform": "qq", "user_id": "u_ask", "chat_id": "c_ask"}
    r1 = await client.post(
        "/api/v1/message/inbound", headers=headers,
        json={**base, "message_id": "ask1", "content": "午饭25"},
    )
    assert r1.json()["status"] == "pending_confirmation"
    assert "账户" in r1.json()["reply_content"]
    r2 = await client.post(
        "/api/v1/message/inbound", headers=headers,
        json={**base, "message_id": "ask2", "content": "微信"},
    )
    assert "什么时候" in r2.json()["reply_content"]
    r3 = await client.post(
        "/api/v1/message/inbound", headers=headers,
        json={**base, "message_id": "ask3", "content": "今天"},
    )
    assert r3.json()["status"] == "success"
    assert "已记录 1 笔" in r3.json()["reply_content"]
    # 补问流程结束后，下一条消息必须是全新记录，不能重放草稿
    r4 = await client.post(
        "/api/v1/message/inbound", headers=headers,
        json={**base, "message_id": "ask4", "content": "今天银行卡收入5000"},
    )
    assert r4.json()["status"] == "success"
    assert "收入" in r4.json()["reply_content"]


async def test_delete_flow(client):
    headers = {"X-Api-Key": "test-api-key"}
    base = {"platform": "qq", "user_id": "u_del", "chat_id": "c_del"}
    await client.post(
        "/api/v1/message/inbound", headers=headers,
        json={**base, "message_id": "del1", "content": "今天微信奶茶15"},
    )
    r = await client.post(
        "/api/v1/message/inbound", headers=headers,
        json={**base, "message_id": "del2", "content": "删除刚才那笔"},
    )
    assert r.json()["status"] == "pending_confirmation"
    assert "确认撤销" in r.json()["reply_content"]
    r2 = await client.post(
        "/api/v1/message/inbound", headers=headers,
        json={**base, "message_id": "del3", "content": "是"},
    )
    assert r2.json()["status"] == "success"
    assert "已撤销" in r2.json()["reply_content"]


async def test_modify_flow(client):
    headers = {"X-Api-Key": "test-api-key"}
    base = {"platform": "qq", "user_id": "u_mod", "chat_id": "c_mod"}
    await client.post(
        "/api/v1/message/inbound", headers=headers,
        json={**base, "message_id": "mod1", "content": "今天微信奶茶15"},
    )
    r = await client.post(
        "/api/v1/message/inbound", headers=headers,
        json={**base, "message_id": "mod2", "content": "刚才那笔不是15，是35"},
    )
    assert r.json()["status"] == "pending_confirmation"
    assert "确认把" in r.json()["reply_content"]
    r2 = await client.post(
        "/api/v1/message/inbound", headers=headers,
        json={**base, "message_id": "mod3", "content": "是"},
    )
    assert "已修改" in r2.json()["reply_content"]


async def test_balance_query_and_stats(client):
    headers = {"X-Api-Key": "test-api-key"}
    base = {"platform": "qq", "user_id": "u_stat", "chat_id": "c_stat"}
    await client.post(
        "/api/v1/message/inbound", headers=headers,
        json={**base, "message_id": "st1", "content": "今天微信吃饭花了25"},
    )
    r = await client.post(
        "/api/v1/message/inbound", headers=headers,
        json={**base, "message_id": "st2", "content": "微信还有多少钱"},
    )
    assert "余额" in r.json()["reply_content"]
    r2 = await client.post(
        "/api/v1/message/inbound", headers=headers,
        json={**base, "message_id": "st3", "content": "今天"},
    )
    assert "支出" in r2.json()["reply_content"]


async def test_query_year_month_balance(client):
    headers = {"X-Api-Key": "test-api-key"}
    base = {"platform": "qq", "user_id": "u_qry", "chat_id": "c_qry"}

    async def send(mid, content):
        r = await client.post(
            "/api/v1/message/inbound", headers=headers,
            json={**base, "message_id": mid, "content": content},
        )
        assert r.status_code == 200
        return r.json()

    await send("q1", "2025年7月5日微信吃饭花了25元")
    await send("q2", "2025年12月8日微信打车花了30元")
    await send("q3", "2026年8月28日微信吃饭花了40元")

    r = await send("q4", "2025年7月份总共花了多少钱")
    assert r["status"] == "success"
    assert "2025-07-01 ~ 2025-07-31" in r["reply_content"]
    assert "25.00" in r["reply_content"]

    r = await send("q5", "生成2025年的账单")
    assert r["status"] == "success"
    assert "2025-01-01 ~ 2025-12-31" in r["reply_content"]
    assert "55.00" in r["reply_content"]

    r = await send("q6", "我现在的余额是多少")
    assert r["status"] == "success"
    assert "余额" in r["reply_content"]

    r = await send("q7", "我总共有多少钱")
    assert r["status"] == "success"
    assert "总资产" in r["reply_content"]

    r = await send("q8", "2025年7月")
    assert r["status"] == "success"
    assert "2025-07-01 ~ 2025-07-31" in r["reply_content"]


async def test_import_confirm_flow(tmp_path):
    from paas import settings_store
    from paas.config import settings as st
    from paas.db import connect, init_db
    from paas.models import Attachment, InboundMessage
    from paas.router import Router

    st.db_path = tmp_path / "imp.db"
    st.secret_key_path = tmp_path / "sk"
    conn = connect()
    init_db(conn)
    settings_store.ensure_default_settings(conn)
    conn.close()
    router = Router()
    csv_data = (
        "日期,金额,分类,备注\n"
        "2026-08-01,86.5,餐饮,吃火锅\n"
        "2026-08-02,23,交通,打车\n"
    ).encode("utf-8-sig")

    async def send(mid, content="", att=None):
        return await router.handle(
            InboundMessage(
                namespace="default", platform="qq", user_id="u_imp", chat_id="c",
                message_id=mid, content=content, attachments=att or [],
            )
        )

    r1 = await send("imp1", att=[Attachment(filename="a.csv", data=csv_data)])
    assert r1.status == "pending_confirmation"
    assert "将新增 2 行" in r1.reply_content
    conn = connect()
    n0 = conn.execute("SELECT COUNT(*) FROM expenses").fetchone()[0]
    conn.close()
    assert n0 == 0  # 确认前账本不变

    r2 = await send("imp2", content="是")
    assert r2.status == "success"
    assert "新增 2 行" in r2.reply_content
    conn = connect()
    n1 = conn.execute("SELECT COUNT(*) FROM expenses").fetchone()[0]
    reply_log = conn.execute(
        "SELECT reply FROM raw_messages WHERE message_id='imp1'"
    ).fetchone()["reply"]
    conn.close()
    assert n1 == 2
    assert "账本预览" in reply_log  # 回复日志已落库

    r3 = await send("imp3", att=[Attachment(filename="a.csv", data=csv_data)])
    assert "跳过 2 行" in r3.reply_content
    r4 = await send("imp4", content="是")
    assert "新增 0 行" in r4.reply_content
    await router.shutdown()


async def test_ai_disabled_and_enabled(client, monkeypatch):
    headers = {"X-Api-Key": "test-api-key"}
    base = {"platform": "qq", "user_id": "u_ai", "chat_id": "c_ai", "content": "用AI：今天微信吃饭花了25"}
    r = await client.post("/api/v1/message/inbound", headers=headers, json={**base, "message_id": "ai0"})
    assert "未启用" in r.json()["reply_content"]

    await client.post(
        "/admin/api/login", json={"username": "admin", "password": "test-admin-pass-123"}
    )
    put = await client.put(
        "/admin/api/ai",
        json={
            "local_enabled": False,
            "local_model": "qwen2.5:0.5b",
            "local_base_url": "http://localhost:11434",
            "cloud_enabled": True,
            "cloud_model": "deepseek-chat",
            "cloud_base_url": "https://api.deepseek.com/v1",
            "api_key": "sk-test",
            "order": "rules,cloud,local",
            "timeout_seconds": "45",
        },
    )
    assert put.status_code == 200
    got = await client.get("/admin/api/ai")
    assert got.json()["has_api_key"] is True
    assert got.json()["cloud_enabled"] is True
    assert got.json()["order"] == "rules,cloud,local"

    seen = []

    async def fake_ai(conn, content, backend, cfg=None):
        seen.append((content, backend))
        calls.append(content)
        return {"date": "今天", "type": "expense", "category": "餐饮", "amount": 25, "account": "微信", "note": "吃饭"}

    calls = []
    monkeypatch.setattr("paas.interpreter.core.ai_interpret", fake_ai)
    r2 = await client.post("/api/v1/message/inbound", headers=headers, json={**base, "message_id": "ai1"})
    assert r2.json()["status"] == "success"
    assert "25.00 元" in r2.json()["reply_content"]
    assert calls == ["今天微信吃饭花了25"]
    assert seen == [("今天微信吃饭花了25", "cloud")]  # 手动触发：跳过规则，云端（顺序中先于本地）

    async def fake_ai2(conn, content, backend, cfg=None):
        calls.append(content)
        return {"date": "今天", "type": "expense", "category": "餐饮", "amount": 35, "account": "", "note": "打车"}

    monkeypatch.setattr("paas.interpreter.core.ai_interpret", fake_ai2)
    r3 = await client.post(
        "/api/v1/message/inbound", headers=headers,
        json={**base, "message_id": "ai2", "content": "用AI：今天微信吃饭花了35"},
    )
    assert r3.json()["status"] == "pending_confirmation"
    assert "账户" in r3.json()["reply_content"]
    assert calls == ["今天微信吃饭花了25", "今天微信吃饭花了35"]


async def test_ai_pull_uses_local_base_url(client, monkeypatch):
    """/admin/api/ai/pull 必须使用 local_base_url（曾误读不存在的 base_url）。"""
    await client.post(
        "/admin/api/login", json={"username": "admin", "password": "test-admin-pass-123"}
    )
    put = await client.put(
        "/admin/api/ai",
        json={
            "local_enabled": True,
            "local_model": "qwen2.5:0.5b",
            "local_base_url": "http://192.168.9.9:11434",
            "cloud_enabled": False,
            "cloud_model": "",
            "cloud_base_url": "",
            "api_key": "",
            "order": "rules,local,cloud",
            "timeout_seconds": "45",
        },
    )
    assert put.status_code == 200

    seen = {}

    async def fake_pull(model, base_url=None):
        seen["model"] = model
        seen["base"] = base_url
        return True, "模型已就绪"

    monkeypatch.setattr("paas.interpreter.core.ollama_pull", fake_pull)
    r = await client.post("/admin/api/ai/pull", json={"model": "qwen2.5:0.5b"})
    assert r.status_code == 200
    assert seen.get("base") == "http://192.168.9.9:11434"
    assert r.json()["ok"] is True


async def test_conversations_api(client):
    headers = {"X-Api-Key": "test-api-key"}
    await client.post(
        "/admin/api/login", json={"username": "admin", "password": "test-admin-pass-123"}
    )
    bot = (await client.post(
        "/admin/api/bots",
        json={"platform": "qq", "name": "会话机器人", "enabled": False, "fields": {"app_id": "1"}},
    )).json()["bot_id"]
    await client.post(
        "/api/v1/message/inbound", headers=headers,
        json={"namespace": bot, "platform": "qq", "user_id": "u_conv", "chat_id": "c",
              "message_id": "cv1", "content": "今天微信吃饭花了25元"},
    )
    r = await client.get("/admin/api/conversations?limit=10")
    assert r.status_code == 200
    convs = r.json()["conversations"]
    assert any(c["message_id"] == "cv1" and "已记录" in (c["reply"] or "") for c in convs)


async def test_balance_to_flow(client):
    headers = {"X-Api-Key": "test-api-key"}
    base = {"platform": "qq", "user_id": "u_bal2", "chat_id": "c_bal2"}
    await client.post(
        "/api/v1/message/inbound", headers=headers,
        json={**base, "message_id": "b1", "content": "今天微信吃饭花了25"},
    )
    r = await client.post(
        "/api/v1/message/inbound", headers=headers,
        json={**base, "message_id": "b2", "content": "微信平账到80"},
    )
    assert r.json()["status"] == "pending_confirmation"
    assert "平账" in r.json()["reply_content"]
    r2 = await client.post(
        "/api/v1/message/inbound", headers=headers,
        json={**base, "message_id": "b3", "content": "是"},
    )
    assert r2.json()["status"] == "success"
    assert "平账" in r2.json()["reply_content"]


async def test_admin_login_and_bots(client):
    resp = await client.post(
        "/admin/api/login", json={"username": "admin", "password": "test-admin-pass-123"}
    )
    assert resp.status_code == 200
    assert "paas_session" in resp.cookies

    me = await client.get("/admin/api/me")
    assert me.json()["username"] == "admin"
    assert me.json()["role"] == "admin"

    create = await client.post(
        "/admin/api/bots",
        json={
            "platform": "qq",
            "name": "测试QQ机器人",
            "enabled": False,
            "fields": {
                "app_id": "123456",
                "app_secret": "secret-abc",
                "default_chat_id": "",
                "chat_scope": "private",
            },
        },
    )
    assert create.status_code == 200
    bot_id = create.json()["bot_id"]

    got = await client.get("/admin/api/bots/" + bot_id)
    assert got.status_code == 200
    cfg = got.json()["config"]
    assert cfg["app_id"] == "123456"
    assert cfg["app_secret"] == "secret-abc"
    assert cfg["has_app_secret"] is True

    # 掩码值保存时保留原值
    put2 = await client.put(
        "/admin/api/bots/" + bot_id,
        json={"enabled": False, "name": "改名", "fields": {"app_secret": "••••••••"}},
    )
    assert put2.status_code == 200
    got2 = await client.get("/admin/api/bots/" + bot_id)
    assert got2.json()["config"]["has_app_secret"] is True


async def test_bot_secret_visible_to_owner(client):
    await client.post(
        "/admin/api/login", json={"username": "admin", "password": "test-admin-pass-123"}
    )
    await client.post(
        "/admin/api/users", json={"username": "alice", "password": "alice-pass-123", "role": "user"}
    )
    await client.post("/admin/api/logout")
    await client.post(
        "/admin/api/login", json={"username": "alice", "password": "alice-pass-123"}
    )
    bot = (await client.post(
        "/admin/api/bots",
        json={"platform": "telegram", "name": "alice的TG", "enabled": False, "fields": {"token": "123:ABC"}},
    )).json()["bot_id"]
    got = await client.get("/admin/api/bots/" + bot)
    assert got.status_code == 200
    cfg = got.json()["config"]
    assert cfg["token"] == "123:ABC"
    assert cfg["has_token"] is True


async def test_bot_limit_and_user_role(client):
    await client.post(
        "/admin/api/login", json={"username": "admin", "password": "test-admin-pass-123"}
    )
    # 创建普通用户
    r = await client.post(
        "/admin/api/users", json={"username": "alice", "password": "alice-pass-123", "role": "user"}
    )
    assert r.status_code == 200
    # 普通用户只能看到自己的机器人
    await client.post("/admin/api/logout")
    await client.post(
        "/admin/api/login", json={"username": "alice", "password": "alice-pass-123"}
    )
    me = await client.get("/admin/api/me")
    assert me.json()["role"] == "user"
    bots = await client.get("/admin/api/bots")
    assert bots.json()["bots"] == []
    # 普通用户无权访问用户管理
    users = await client.get("/admin/api/users")
    assert users.status_code == 403
    # 普通用户创建机器人
    create = await client.post(
        "/admin/api/bots",
        json={"platform": "telegram", "name": "alice的TG", "enabled": False, "fields": {"token": "123:ABC"}},
    )
    assert create.status_code == 200
    # 管理员能看到所有人
    await client.post("/admin/api/logout")
    await client.post(
        "/admin/api/login", json={"username": "admin", "password": "test-admin-pass-123"}
    )
    bots = await client.get("/admin/api/bots")
    assert len(bots.json()["bots"]) >= 1


async def test_multi_bot_data_isolation(client):
    headers = {"X-Api-Key": "test-api-key"}
    await client.post(
        "/admin/api/login", json={"username": "admin", "password": "test-admin-pass-123"}
    )
    b1 = (await client.post(
        "/admin/api/bots",
        json={"platform": "qq", "name": "机器人1", "enabled": False, "fields": {"app_id": "1"}},
    )).json()["bot_id"]
    b2 = (await client.post(
        "/admin/api/bots",
        json={"platform": "qq", "name": "机器人2", "enabled": False, "fields": {"app_id": "2"}},
    )).json()["bot_id"]

    def inbound(ns, mid, content):
        return client.post(
            "/api/v1/message/inbound",
            headers=headers,
            json={"namespace": ns, "platform": "qq", "user_id": "u_x", "chat_id": "c_x",
                  "message_id": mid, "content": content},
        )

    r1 = await inbound(b1, "iso1", "今天微信吃饭花了25元")
    r2 = await inbound(b2, "iso2", "今天微信吃饭花了88元")
    assert r1.json()["status"] == "success"
    assert r2.json()["status"] == "success"

    # 两个命名空间数据互不可见
    q1 = await inbound(b1, "iso3", "微信还有多少钱")
    q2 = await inbound(b2, "iso4", "微信还有多少钱")
    assert "-25.00" in q1.json()["reply_content"]
    assert "-88.00" in q2.json()["reply_content"]

    # 删除机器人2 → 其数据一并清除
    d = await client.delete("/admin/api/bots/" + b2)
    assert d.status_code == 200
    q2b = await inbound(b2, "iso5", "微信还有多少钱")
    assert "还没有账户数据" in q2b.json()["reply_content"]


async def test_admin_settings_and_template(client):
    await client.post(
        "/admin/api/login", json={"username": "admin", "password": "test-admin-pass-123"}
    )
    put = await client.put(
        "/admin/api/settings",
        json={"updates": {"dedup_window_seconds": "45", "timezone": "Asia/Hong_Kong"}},
    )
    assert put.status_code == 200
    assert "dedup_window_seconds" in put.json()["applied"]
    got = await client.get("/admin/api/settings")
    assert got.json()["dedup_window_seconds"] == "45"
    assert got.json()["timezone"] == "Asia/Hong_Kong"

    tmpl = await client.get("/admin/api/import-template")
    assert tmpl.status_code == 200
    assert "日期" in tmpl.content.decode("utf-8-sig")


async def test_admin_backup_and_status(client):
    await client.post(
        "/admin/api/login", json={"username": "admin", "password": "test-admin-pass-123"}
    )
    status = await client.get("/admin/api/status")
    assert status.status_code == 200
    assert status.json()["scheduler_running"] is False
    backup = await client.post("/admin/api/backup")
    assert backup.status_code == 200
    assert backup.json()["ok"] is True


async def test_admin_wrong_password(client):
    resp = await client.post(
        "/admin/api/login", json={"username": "admin", "password": "wrong-password"}
    )
    assert resp.status_code == 401


async def test_admin_change_password(client):
    await client.post(
        "/admin/api/login", json={"username": "admin", "password": "test-admin-pass-123"}
    )
    resp = await client.post(
        "/admin/api/password",
        json={"old_password": "test-admin-pass-123", "new_password": "brand-new-pass-456"},
    )
    assert resp.status_code == 200
    # 旧密码失效
    r2 = await client.post(
        "/admin/api/login", json={"username": "admin", "password": "test-admin-pass-123"}
    )
    assert r2.status_code == 401


async def test_per_user_bot_limit(client):
    await client.post(
        "/admin/api/login", json={"username": "admin", "password": "test-admin-pass-123"}
    )
    await client.post(
        "/admin/api/users", json={"username": "alice", "password": "alice-pass-123", "role": "user"}
    )
    await client.post(
        "/admin/api/users", json={"username": "bob", "password": "bob-pass-123", "role": "user"}
    )
    # 用户 A 每平台最多 5 个
    await client.post("/admin/api/logout")
    await client.post(
        "/admin/api/login", json={"username": "alice", "password": "alice-pass-123"}
    )
    for i in range(5):
        r = await client.post(
            "/admin/api/bots",
            json={"platform": "qq", "name": f"alice-bot-{i}", "enabled": False, "fields": {"app_id": str(i)}},
        )
        assert r.status_code == 200, r.text
    sixth = await client.post(
        "/admin/api/bots",
        json={"platform": "qq", "name": "alice-bot-6", "enabled": False, "fields": {"app_id": "6"}},
    )
    assert sixth.status_code == 400
    # 用户 B 不受用户 A 数量影响，仍可创建 QQ 机器人
    await client.post("/admin/api/logout")
    await client.post(
        "/admin/api/login", json={"username": "bob", "password": "bob-pass-123"}
    )
    r = await client.post(
        "/admin/api/bots",
        json={"platform": "qq", "name": "bob-bot", "enabled": False, "fields": {"app_id": "bob"}},
    )
    assert r.status_code == 200
    # 管理员创建自己的 QQ 机器人不受用户 A 数量影响
    await client.post("/admin/api/logout")
    await client.post(
        "/admin/api/login", json={"username": "admin", "password": "test-admin-pass-123"}
    )
    r = await client.post(
        "/admin/api/bots",
        json={"platform": "qq", "name": "admin-bot", "enabled": False, "fields": {"app_id": "admin"}},
    )
    assert r.status_code == 200


async def test_bot_list_isolation(client):
    await client.post(
        "/admin/api/login", json={"username": "admin", "password": "test-admin-pass-123"}
    )
    await client.post(
        "/admin/api/users", json={"username": "alice", "password": "alice-pass-123", "role": "user"}
    )
    admin_bot = (await client.post(
        "/admin/api/bots",
        json={"platform": "telegram", "name": "admin的TG", "enabled": False, "fields": {"token": "1:AAA"}},
    )).json()["bot_id"]
    # 普通用户看不到管理员的机器人
    await client.post("/admin/api/logout")
    await client.post(
        "/admin/api/login", json={"username": "alice", "password": "alice-pass-123"}
    )
    alice_bot = (await client.post(
        "/admin/api/bots",
        json={"platform": "telegram", "name": "alice的TG", "enabled": False, "fields": {"token": "2:BBB"}},
    )).json()["bot_id"]
    bots = (await client.get("/admin/api/bots")).json()["bots"]
    assert [b["bot_id"] for b in bots] == [alice_bot]
    # 管理员看到全部机器人且带归属
    await client.post("/admin/api/logout")
    await client.post(
        "/admin/api/login", json={"username": "admin", "password": "test-admin-pass-123"}
    )
    all_bots = (await client.get("/admin/api/bots")).json()["bots"]
    by_id = {b["bot_id"]: b for b in all_bots}
    assert admin_bot in by_id and alice_bot in by_id
    assert by_id[admin_bot]["owner_name"] == "admin"
    assert by_id[alice_bot]["owner_name"] == "alice"


async def test_user_data_scoping(client, conn):
    headers = {"X-Api-Key": "test-api-key"}
    await client.post(
        "/admin/api/login", json={"username": "admin", "password": "test-admin-pass-123"}
    )
    await client.post(
        "/admin/api/users", json={"username": "alice", "password": "alice-pass-123", "role": "user"}
    )
    admin_bot = (await client.post(
        "/admin/api/bots",
        json={"platform": "qq", "name": "admin的QQ", "enabled": False, "fields": {"app_id": "1"}},
    )).json()["bot_id"]
    await client.post("/admin/api/logout")
    await client.post(
        "/admin/api/login", json={"username": "alice", "password": "alice-pass-123"}
    )
    alice_bot = (await client.post(
        "/admin/api/bots",
        json={"platform": "qq", "name": "alice的QQ", "enabled": False, "fields": {"app_id": "2"}},
    )).json()["bot_id"]

    # 流水：admin 的机器人 1 笔，alice 的机器人 2 笔
    await client.post(
        "/api/v1/message/inbound", headers=headers,
        json={"namespace": admin_bot, "platform": "qq", "user_id": "u_admin", "chat_id": "c",
              "message_id": "adm1", "content": "今天微信吃饭花了25元"},
    )
    await client.post(
        "/api/v1/message/inbound", headers=headers,
        json={"namespace": alice_bot, "platform": "qq", "user_id": "u_ali", "chat_id": "c",
              "message_id": "ali1", "content": "今天微信吃饭花了25元"},
    )
    await client.post(
        "/api/v1/message/inbound", headers=headers,
        json={"namespace": alice_bot, "platform": "qq", "user_id": "u_ali", "chat_id": "c",
              "message_id": "ali2", "content": "今天支付宝打车花了30"},
    )
    # 导入记录：直接写入模拟
    conn.execute(
        "INSERT INTO imports (namespace, user_id, platform, message_id, filename, file_type, "
        "total_rows, success_rows, failed_rows) VALUES (?,?,?,?,?,?,?,?,?)",
        (admin_bot, "u_admin", "qq", "imp_a", "admin.csv", "csv", 3, 3, 0),
    )
    conn.execute(
        "INSERT INTO imports (namespace, user_id, platform, message_id, filename, file_type, "
        "total_rows, success_rows, failed_rows) VALUES (?,?,?,?,?,?,?,?,?)",
        (alice_bot, "u_ali", "qq", "imp_b", "alice.csv", "csv", 5, 5, 0),
    )
    conn.commit()

    # alice 视角：只有自己的机器人与数据
    st = (await client.get("/admin/api/status")).json()
    assert st["bots"] == 1
    assert st["expenses"] == 2
    assert st["imports"] == 1
    im = (await client.get("/admin/api/imports")).json()["imports"]
    assert [r["filename"] for r in im] == ["alice.csv"]
    cv = (await client.get("/admin/api/conversations?limit=50")).json()["conversations"]
    msgs = {c["message_id"] for c in cv}
    assert {"ali1", "ali2"} <= msgs
    assert "adm1" not in msgs

    # admin 视角：全部状态与记录，但最近对话只有自己的
    await client.post("/admin/api/logout")
    await client.post(
        "/admin/api/login", json={"username": "admin", "password": "test-admin-pass-123"}
    )
    st = (await client.get("/admin/api/status")).json()
    assert st["bots"] == 2
    assert st["expenses"] == 3
    assert st["imports"] == 2
    im = (await client.get("/admin/api/imports")).json()["imports"]
    assert len(im) == 2
    cv = (await client.get("/admin/api/conversations?limit=50")).json()["conversations"]
    msgs = {c["message_id"] for c in cv}
    assert "adm1" in msgs
    assert "ali1" not in msgs and "ali2" not in msgs


async def test_ownership_checks(client):
    await client.post(
        "/admin/api/login", json={"username": "admin", "password": "test-admin-pass-123"}
    )
    await client.post(
        "/admin/api/users", json={"username": "alice", "password": "alice-pass-123", "role": "user"}
    )
    admin_bot = (await client.post(
        "/admin/api/bots",
        json={"platform": "qq", "name": "admin的QQ", "enabled": False, "fields": {"app_id": "1"}},
    )).json()["bot_id"]
    await client.post("/admin/api/logout")
    await client.post(
        "/admin/api/login", json={"username": "alice", "password": "alice-pass-123"}
    )
    alice_bot = (await client.post(
        "/admin/api/bots",
        json={"platform": "qq", "name": "alice的QQ", "enabled": False, "fields": {"app_id": "2"}},
    )).json()["bot_id"]

    # alice 访问管理员的机器人 → 一律 403
    assert (await client.get(f"/admin/api/bots/{admin_bot}/accounts")).status_code == 403
    assert (await client.put(
        f"/admin/api/bots/{admin_bot}/accounts", json={"templates": []}
    )).status_code == 403
    assert (await client.post(
        f"/admin/api/bots/{admin_bot}/test", json={"fields": {"app_id": "1"}}
    )).status_code == 403
    assert (await client.get(
        f"/admin/api/backfill/preview?namespace={admin_bot}&user_id=u&keyword=k"
    )).status_code == 403
    assert (await client.post(
        "/admin/api/backfill/apply",
        json={"namespace": admin_bot, "user_id": "u", "mappings": [{"keyword": "k", "account": "a"}]},
    )).status_code == 403
    assert (await client.get(f"/admin/api/export?namespace={admin_bot}&user_id=u")).status_code == 403

    # alice 自己的机器人 → 正常
    assert (await client.get(f"/admin/api/bots/{alice_bot}/accounts")).status_code == 200
    assert (await client.get(
        f"/admin/api/backfill/preview?namespace={alice_bot}&user_id=u&keyword=k"
    )).status_code == 200


async def test_settings_admin_only(client):
    await client.post(
        "/admin/api/login", json={"username": "admin", "password": "test-admin-pass-123"}
    )
    await client.post(
        "/admin/api/users", json={"username": "alice", "password": "alice-pass-123", "role": "user"}
    )
    await client.post("/admin/api/logout")
    await client.post(
        "/admin/api/login", json={"username": "alice", "password": "alice-pass-123"}
    )
    # 普通用户：设置与 AI 全部 403
    assert (await client.get("/admin/api/settings")).status_code == 403
    assert (await client.put(
        "/admin/api/settings", json={"updates": {"dedup_window_seconds": "45"}}
    )).status_code == 403
    assert (await client.get("/admin/api/ai")).status_code == 403
    assert (await client.put("/admin/api/ai", json={})).status_code == 403
    assert (await client.post("/admin/api/ai/status")).status_code == 403
    assert (await client.post("/admin/api/ai/pull", json={})).status_code == 403
    assert (await client.post(
        "/admin/api/ai/local/delete", json={"model": "qwen2.5:0.5b", "confirm": "删除本地模型"}
    )).status_code == 403
    # 管理员正常
    await client.post("/admin/api/logout")
    await client.post(
        "/admin/api/login", json={"username": "admin", "password": "test-admin-pass-123"}
    )
    assert (await client.get("/admin/api/settings")).status_code == 200
    assert (await client.get("/admin/api/ai")).status_code == 200


async def test_ai_me_default_off(client):
    await client.post(
        "/admin/api/login", json={"username": "admin", "password": "test-admin-pass-123"}
    )
    await client.post(
        "/admin/api/users", json={"username": "alice", "password": "alice-pass-123", "role": "user"}
    )
    await client.post("/admin/api/logout")
    await client.post(
        "/admin/api/login", json={"username": "alice", "password": "alice-pass-123"}
    )
    a = (await client.get("/admin/api/ai/me")).json()
    assert a["local_enabled"] is False
    assert a["cloud_enabled"] is False
    assert a["has_api_key"] is False
    assert a["order"] == "rules,local,cloud"
    assert a["local_model"] == "qwen2.5:0.5b"


async def test_ai_me_user_isolation_and_ignored_fields(client):
    await client.post(
        "/admin/api/login", json={"username": "admin", "password": "test-admin-pass-123"}
    )
    await client.post(
        "/admin/api/users", json={"username": "alice", "password": "alice-pass-123", "role": "user"}
    )
    await client.post(
        "/admin/api/users", json={"username": "bob", "password": "bob-pass-123", "role": "user"}
    )
    await client.post("/admin/api/logout")
    await client.post(
        "/admin/api/login", json={"username": "alice", "password": "alice-pass-123"}
    )
    put = await client.put("/admin/api/ai/me", json={
        "local_enabled": True,
        "local_model": "evil-model",
        "local_base_url": "http://evil:11434",
        "cloud_enabled": True,
        "cloud_model": "deepseek-chat",
        "cloud_base_url": "https://api.deepseek.com/v1",
        "api_key": "sk-alice",
        "order": "cloud,rules,local",
        "timeout_seconds": "999",
    })
    assert put.status_code == 200
    a = (await client.get("/admin/api/ai/me")).json()
    assert a["local_enabled"] is True
    assert a["cloud_enabled"] is True
    assert a["has_api_key"] is True
    assert a["cloud_model"] == "deepseek-chat"
    assert a["order"] == "cloud,rules,local"
    # 本地模型名/地址与超时由服务器统一配置，普通用户提交被忽略
    assert a["local_model"] == "qwen2.5:0.5b"
    assert a["timeout_seconds"] == "45"
    # bob 看不到 alice 的配置
    await client.post("/admin/api/logout")
    await client.post(
        "/admin/api/login", json={"username": "bob", "password": "bob-pass-123"}
    )
    b = (await client.get("/admin/api/ai/me")).json()
    assert b["local_enabled"] is False
    assert b["has_api_key"] is False
    assert b["cloud_model"] == ""
    assert b["order"] == "rules,local,cloud"
    # 管理员全局配置不受 alice 影响
    await client.post("/admin/api/logout")
    await client.post(
        "/admin/api/login", json={"username": "admin", "password": "test-admin-pass-123"}
    )
    g = (await client.get("/admin/api/ai")).json()
    assert g["local_enabled"] is False
    assert g["has_api_key"] is False
    # 普通用户无权访问全局 AI 接口
    await client.post("/admin/api/logout")
    await client.post(
        "/admin/api/login", json={"username": "alice", "password": "alice-pass-123"}
    )
    assert (await client.get("/admin/api/ai")).status_code == 403
    assert (await client.put("/admin/api/ai", json={})).status_code == 403
    # API Key 掩码/留空保留原值
    r = await client.put("/admin/api/ai/me", json={
        "local_enabled": True,
        "cloud_enabled": True,
        "cloud_model": "deepseek-chat",
        "cloud_base_url": "https://api.deepseek.com/v1",
        "api_key": "••••••••",
        "order": "cloud,rules,local",
    })
    assert r.status_code == 200
    a2 = (await client.get("/admin/api/ai/me")).json()
    assert a2["has_api_key"] is True


async def test_delete_user_cleans_ai_settings(client, conn):
    await client.post(
        "/admin/api/login", json={"username": "admin", "password": "test-admin-pass-123"}
    )
    await client.post(
        "/admin/api/users", json={"username": "alice", "password": "alice-pass-123", "role": "user"}
    )
    alice_id = conn.execute(
        "SELECT id FROM admin_users WHERE username = 'alice'"
    ).fetchone()["id"]
    await client.post("/admin/api/logout")
    await client.post(
        "/admin/api/login", json={"username": "alice", "password": "alice-pass-123"}
    )
    await client.put("/admin/api/ai/me", json={
        "local_enabled": True,
        "cloud_enabled": True,
        "cloud_model": "m",
        "cloud_base_url": "https://x",
        "api_key": "sk-alice",
        "order": "rules,cloud,local",
    })
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM user_ai_settings WHERE user_id = ?", (alice_id,)
    ).fetchone()["n"] == 1
    await client.post("/admin/api/logout")
    await client.post(
        "/admin/api/login", json={"username": "admin", "password": "test-admin-pass-123"}
    )
    d = await client.delete(f"/admin/api/users/{alice_id}")
    assert d.status_code == 200
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM user_ai_settings WHERE user_id = ?", (alice_id,)
    ).fetchone()["n"] == 0


async def test_effective_ai_settings_scoped(client, conn):
    from paas.modules.admin import service as admin_service
    from paas.security import decrypt_json

    await client.post(
        "/admin/api/login", json={"username": "admin", "password": "test-admin-pass-123"}
    )
    await client.post(
        "/admin/api/users", json={"username": "alice", "password": "alice-pass-123", "role": "user"}
    )
    await client.post(
        "/admin/api/users", json={"username": "bob", "password": "bob-pass-123", "role": "user"}
    )
    admin_bot = (await client.post(
        "/admin/api/bots",
        json={"platform": "qq", "name": "admin的QQ", "enabled": False, "fields": {"app_id": "1"}},
    )).json()["bot_id"]
    await client.post("/admin/api/logout")
    await client.post(
        "/admin/api/login", json={"username": "alice", "password": "alice-pass-123"}
    )
    alice_bot = (await client.post(
        "/admin/api/bots",
        json={"platform": "qq", "name": "alice的QQ", "enabled": False, "fields": {"app_id": "2"}},
    )).json()["bot_id"]
    await client.put("/admin/api/ai/me", json={
        "local_enabled": True,
        "cloud_enabled": True,
        "cloud_model": "deepseek-chat",
        "cloud_base_url": "https://api.deepseek.com/v1",
        "api_key": "sk-alice",
        "order": "cloud,rules,local",
    })
    await client.post("/admin/api/logout")
    await client.post(
        "/admin/api/login", json={"username": "bob", "password": "bob-pass-123"}
    )
    bob_bot = (await client.post(
        "/admin/api/bots",
        json={"platform": "qq", "name": "bob的QQ", "enabled": False, "fields": {"app_id": "3"}},
    )).json()["bot_id"]

    # alice 的机器人：用自己的 key 与顺序
    cfg = admin_service.effective_ai_settings(conn, alice_bot)
    assert cfg["local_enabled"] is True
    assert cfg["cloud_enabled"] is True
    assert decrypt_json(cfg["api_key_enc"])["key"] == "sk-alice"
    assert cfg["order"] == "cloud,rules,local"
    assert cfg["local_model"] == "qwen2.5:0.5b"
    # 管理员的机器人：全局配置（默认无云端 key）
    cfg_admin = admin_service.effective_ai_settings(conn, admin_bot)
    assert cfg_admin["cloud_enabled"] is False
    assert cfg_admin["api_key_enc"] == ""
    # 未配置的普通用户：默认全关
    cfg_bob = admin_service.effective_ai_settings(conn, bob_bot)
    assert cfg_bob["local_enabled"] is False
    assert cfg_bob["cloud_enabled"] is False
    assert cfg_bob["api_key_enc"] == ""
    assert cfg_bob["order"] == "rules,local,cloud"
    # 未知命名空间：回退全局
    cfg_default = admin_service.effective_ai_settings(conn, "default")
    assert cfg_default["local_model"] == "qwen2.5:0.5b"


async def test_router_uses_owner_ai_settings(client, monkeypatch):
    from paas.security import decrypt_json

    headers = {"X-Api-Key": "test-api-key"}
    await client.post(
        "/admin/api/login", json={"username": "admin", "password": "test-admin-pass-123"}
    )
    await client.post(
        "/admin/api/users", json={"username": "alice", "password": "alice-pass-123", "role": "user"}
    )
    await client.post(
        "/admin/api/users", json={"username": "bob", "password": "bob-pass-123", "role": "user"}
    )
    admin_bot = (await client.post(
        "/admin/api/bots",
        json={"platform": "qq", "name": "admin的QQ", "enabled": False, "fields": {"app_id": "1"}},
    )).json()["bot_id"]
    await client.post("/admin/api/logout")
    await client.post(
        "/admin/api/login", json={"username": "alice", "password": "alice-pass-123"}
    )
    alice_bot = (await client.post(
        "/admin/api/bots",
        json={"platform": "qq", "name": "alice的QQ", "enabled": False, "fields": {"app_id": "2"}},
    )).json()["bot_id"]
    await client.put("/admin/api/ai/me", json={
        "local_enabled": False,
        "cloud_enabled": True,
        "cloud_model": "deepseek-chat",
        "cloud_base_url": "https://api.deepseek.com/v1",
        "api_key": "sk-alice",
        "order": "cloud,rules,local",
    })
    await client.post("/admin/api/logout")
    await client.post(
        "/admin/api/login", json={"username": "bob", "password": "bob-pass-123"}
    )
    bob_bot = (await client.post(
        "/admin/api/bots",
        json={"platform": "qq", "name": "bob的QQ", "enabled": False, "fields": {"app_id": "3"}},
    )).json()["bot_id"]
    await client.post("/admin/api/logout")
    await client.post(
        "/admin/api/login", json={"username": "admin", "password": "test-admin-pass-123"}
    )
    await client.put("/admin/api/ai", json={
        "local_enabled": False,
        "local_model": "qwen2.5:0.5b",
        "local_base_url": "http://localhost:11434",
        "cloud_enabled": True,
        "cloud_model": "admin-chat",
        "cloud_base_url": "https://api.admin.example/v1",
        "api_key": "sk-admin",
        "order": "rules,cloud,local",
        "timeout_seconds": "45",
    })

    captured = []

    async def fake_ai(conn, content, backend, cfg=None):
        captured.append((backend, cfg))
        return {"date": "今天", "type": "expense", "category": "餐饮", "amount": 25, "account": "微信", "note": "吃饭"}

    monkeypatch.setattr("paas.interpreter.core.ai_interpret", fake_ai)

    # alice 的机器人：使用 alice 自己的 key
    r = await client.post(
        "/api/v1/message/inbound", headers=headers,
        json={"namespace": alice_bot, "platform": "qq", "user_id": "u_a", "chat_id": "c",
              "message_id": "ai_a1", "content": "用AI：今天微信吃饭花了25"},
    )
    assert r.json()["status"] == "success"
    backend, cfg = captured[-1]
    assert backend == "cloud"
    assert decrypt_json(cfg["api_key_enc"])["key"] == "sk-alice"
    # 管理员的机器人：使用全局（管理员本人）的 key
    captured.clear()
    r2 = await client.post(
        "/api/v1/message/inbound", headers=headers,
        json={"namespace": admin_bot, "platform": "qq", "user_id": "u_adm", "chat_id": "c",
              "message_id": "ai_adm1", "content": "用AI：今天微信吃饭花了25"},
    )
    assert r2.json()["status"] == "success"
    backend2, cfg2 = captured[-1]
    assert decrypt_json(cfg2["api_key_enc"])["key"] == "sk-admin"
    # 未配置的普通用户：AI 未启用，不调用任何 AI 后端
    captured.clear()
    r3 = await client.post(
        "/api/v1/message/inbound", headers=headers,
        json={"namespace": bob_bot, "platform": "qq", "user_id": "u_b", "chat_id": "c",
              "message_id": "ai_b1", "content": "用AI：今天微信吃饭花了25"},
    )
    assert "未启用" in r3.json()["reply_content"]
    assert captured == []
