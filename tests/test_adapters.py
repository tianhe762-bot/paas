import httpx
import pytest

from paas.communication.qq.api import QQApiClient
from paas.communication.qq.ws import QQBotClient
from paas.communication.telegram.adapter import TelegramAdapter
from paas.models import Reply


def _qq_transport(calls: list):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(
            (request.method, request.url.path, request.headers.get("Authorization"), request.content)
        )
        if request.url.path.endswith("/app/getAppAccessToken"):
            return httpx.Response(200, json={"access_token": "tok123", "expires_in": 7200})
        if request.url.path == "/gateway/bot":
            return httpx.Response(200, json={"url": "wss://example.invalid/websocket"})
        if "/v2/users/" in request.url.path and request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"id": "msg-out", "timestamp": "2026-08-28T12:00:00+08:00"})
        return httpx.Response(404, json={"err_code": 404})

    return handler


async def test_qq_api_flow():
    calls: list = []
    client = QQApiClient(
        app_id="123456",
        app_secret="secret",
        api_base="https://api.bot.qq.com",
        token_url="https://bots.qq.com/app/getAppAccessToken",
    )
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(_qq_transport(calls)))
    token = await client.get_access_token()
    assert token == "tok123"
    gateway = await client.get_gateway_url()
    assert gateway.endswith("/websocket")
    resp = await client.send_c2c_message("openid-1", "你好", msg_id="m1", msg_seq=1)
    assert resp["id"] == "msg-out"
    auths = [c[2] for c in calls if c[1] == "/v2/users/openid-1/messages"]
    assert auths == ["QQBot tok123"]
    body_calls = [c[3] for c in calls if c[1] == "/v2/users/openid-1/messages"]
    assert b'"msg_type":0' in body_calls[0]
    assert b'"msg_id":"m1"' in body_calls[0]
    await client.aclose()


async def test_qq_dispatch_and_reply():
    received: dict = {}

    async def handler(msg):
        received["user_id"] = msg.user_id
        received["content"] = msg.content
        received["attachments"] = msg.attachments
        return Reply(status="success", reply_content="✅ 已记录")

    calls: list = []
    api = QQApiClient(
        app_id="123456",
        app_secret="secret",
        api_base="https://api.bot.qq.com",
        token_url="https://bots.qq.com/app/getAppAccessToken",
    )
    api._client = httpx.AsyncClient(transport=httpx.MockTransport(_qq_transport(calls)))
    bot = QQBotClient(api, handler=handler)
    await bot._on_dispatch("READY", {"session_id": "sess-1"})
    assert bot._session_id == "sess-1"

    event = {
        "id": "ROBOT1.0_abc",
        "author": {"id": "openid-1", "user_openid": "openid-1"},
        "content": "今天午饭35",
        "timestamp": "2026-08-28T12:00:00+08:00",
        "message_type": 0,
        "attachments": [
            {"url": "https://example.com/f.csv", "filename": "f.csv", "content_type": "file"}
        ],
    }
    await bot._on_dispatch("C2C_MESSAGE_CREATE", event)
    assert received["user_id"] == "openid-1"
    assert received["content"] == "今天午饭35"
    assert len(received["attachments"]) == 1
    # 相同 msg_id 重复推送只处理一次
    received.clear()
    await bot._on_dispatch("C2C_MESSAGE_CREATE", event)
    assert received == {}
    # 回复已通过 OpenAPI 发出
    assert any(c[1] == "/v2/users/openid-1/messages" for c in calls)
    await api.aclose()


async def test_telegram_process_and_send():
    received: dict = {}

    async def handler(msg):
        received["user_id"] = msg.user_id
        received["content"] = msg.content
        received["file_data"] = (
            msg.attachments[0].data if msg.attachments else None
        )
        return Reply(status="success", reply_content="✅ 收到")

    send_calls: list = []

    def tg_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getUpdates"):
            return httpx.Response(200, json={"ok": True, "result": []})
        if request.url.path.endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "docs/a.csv"}})
        if "/file/bot" in request.url.path:
            return httpx.Response(
                200, content="日期,金额,分类,备注\n2026-08-01,10,餐饮,早餐\n".encode("utf-8")
            )
        if request.url.path.endswith("/sendMessage"):
            send_calls.append(request.content)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 9}})
        return httpx.Response(404, json={"ok": False})

    adapter = TelegramAdapter(token="test-token", handler=handler)
    adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(tg_handler))
    await adapter._process_message(
        {
            "message_id": 1,
            "chat": {"id": 123456},
            "from": {"id": 789012},
            "date": 1756000000,
            "text": "今天午饭35",
            "document": {"file_name": "a.csv", "file_id": "file-1", "mime_type": "text/csv"},
        }
    )
    assert received["user_id"] == "789012"
    assert received["content"] == "今天午饭35"
    assert received["file_data"] is not None
    assert received["file_data"].startswith("日期".encode("utf-8"))
    assert send_calls and "✅ 收到".encode("utf-8") in send_calls[0]
    await adapter.stop()


async def test_telegram_test_connection():
    def tg_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getMe"):
            return httpx.Response(
                200,
                json={"ok": True, "result": {"username": "paas_test_bot", "id": 1}},
            )
        return httpx.Response(404, json={"ok": False})

    adapter = TelegramAdapter(token="test-token")
    adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(tg_handler))
    ok, msg = await adapter.test()
    assert ok is True
    assert "paas_test_bot" in msg
    await adapter.stop()
