import pytest
from httpx import ASGITransport, AsyncClient


def _prepare_app(db_path):
    from paas.config import settings
    from paas import settings_store
    from paas.communication.registry import AdapterManager
    from paas.db import connect, init_db
    from paas.modules.admin import service as admin_service
    from paas.router import Router

    settings.db_path = db_path
    settings.secret_key_path = db_path.parent / "secret.key"
    conn = connect()
    try:
        init_db(conn)
        settings_store.ensure_default_settings(conn)
        admin_service.ensure_admin(conn)
    finally:
        conn.close()

    import paas.main as main

    router = Router()
    main.app.state.router = router
    main.app.state.manager = AdapterManager(router)
    main.app.state.scheduler = None
    return main.app


@pytest.fixture()
async def client(tmp_path):
    app = _prepare_app(tmp_path / "test.db")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await app.state.router.shutdown()


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


async def test_admin_login_and_config(client):
    resp = await client.post(
        "/admin/api/login", json={"username": "admin", "password": "test-admin-pass-123"}
    )
    assert resp.status_code == 200
    assert "paas_session" in resp.cookies

    me = await client.get("/admin/api/me")
    assert me.json()["username"] == "admin"

    cfg = await client.get("/admin/api/config/qq")
    assert cfg.status_code == 200

    put = await client.put(
        "/admin/api/config/qq",
        json={
            "enabled": False,
            "fields": {
                "app_id": "123456",
                "app_secret": "secret-abc",
                "default_chat_id": "",
                "chat_scope": "private",
            },
        },
    )
    assert put.status_code == 200

    got = await client.get("/admin/api/config/qq")
    fields = got.json()["fields"]
    assert fields["app_id"] == "123456"
    assert fields["has_app_secret"] is True
    assert fields["app_secret"] == ""

    # 掩码值保存时保留原值
    put2 = await client.put(
        "/admin/api/config/qq",
        json={"enabled": False, "fields": {"app_id": "123456", "app_secret": "••••••••"}},
    )
    assert put2.status_code == 200


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
