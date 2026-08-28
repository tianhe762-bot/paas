import pytest


def _prepare_conn(tmp_path, ai_local="1", ai_cloud="1", order="rules,local,cloud"):
    from paas import settings_store
    from paas.config import settings
    from paas.db import connect, init_db

    settings.db_path = tmp_path / "ai.db"
    settings.data_dir = tmp_path / "data"
    settings.secret_key_path = tmp_path / "k"
    conn = connect()
    init_db(conn)
    settings_store.ensure_default_settings(conn)
    settings_store.set_many(
        conn,
        {
            "ai_local_enabled": ai_local,
            "ai_cloud_enabled": ai_cloud,
            "ai_order": order,
            "ai_local_base_url": "http://localhost:11434",
            "ai_cloud_base_url": "https://api.example.com/v1",
            "ai_cloud_api_key": "x",
            "ai_cloud_model": "m",
        },
    )
    return conn


def _categories(conn):
    from paas.modules.account.service import load_categories

    return load_categories(conn)


async def test_chain_rules_first(tmp_path, monkeypatch):
    from paas.interpreter.core import interpret

    conn = _prepare_conn(tmp_path)
    called = []

    async def fake_ai(conn_, content, backend, cfg=None):
        called.append(backend)
        return {"date": "今天", "type": "expense", "category": "餐饮", "amount": 25, "account": "微信", "note": "吃饭"}

    monkeypatch.setattr("paas.interpreter.core.ai_interpret", fake_ai)
    engine, items, _ = await interpret(conn, "今天微信吃饭花了25", categories=_categories(conn))
    assert engine == "rules"
    assert items and items[0].amount_cents == 2500
    assert called == []
    conn.close()


async def test_chain_local_then_cloud(tmp_path, monkeypatch):
    from paas.interpreter.core import interpret

    conn = _prepare_conn(tmp_path, order="local,cloud,rules")
    called = []

    async def fake_ai(conn_, content, backend, cfg=None):
        called.append(backend)
        if backend == "local":
            raise RuntimeError("本地失败")
        return {"date": "今天", "type": "expense", "category": "交通", "amount": 18, "account": "微信", "note": "打车"}

    monkeypatch.setattr("paas.interpreter.core.ai_interpret", fake_ai)
    engine, items, _ = await interpret(conn, "今天花了点钱", categories=_categories(conn))
    assert engine == "cloud"  # 本地优先但失败 → 云端兜底
    assert called == ["local", "cloud"]
    assert items[0].amount_cents == 1800
    conn.close()


async def test_chain_all_fail(tmp_path, monkeypatch):
    from paas.interpreter.core import interpret

    conn = _prepare_conn(tmp_path, order="cloud,local,rules")

    async def fake_ai(conn_, content, backend, cfg=None):
        raise RuntimeError("AI 挂了")

    monkeypatch.setattr("paas.interpreter.core.ai_interpret", fake_ai)
    engine, items, error = await interpret(conn, "今天花了点钱", categories=_categories(conn))
    assert engine is None and items is None
    assert "AI 挂了" in (error or "")
    conn.close()


async def test_chain_forced_ai_skips_rules(tmp_path, monkeypatch):
    from paas.interpreter.core import interpret

    conn = _prepare_conn(tmp_path, order="rules,local,cloud")
    called = []

    async def fake_ai(conn_, content, backend, cfg=None):
        called.append(backend)
        return {"date": "今天", "type": "expense", "category": "餐饮", "amount": 25, "account": "微信", "note": "吃饭"}

    monkeypatch.setattr("paas.interpreter.core.ai_interpret", fake_ai)
    engine, items, _ = await interpret(
        conn, "今天微信吃饭花了25", categories=_categories(conn), forced_ai=True
    )
    assert engine == "local"  # 手动触发跳过规则，本地先于云端
    assert called == ["local"]
    conn.close()


async def test_migrate_old_ai_mode(tmp_path):
    from paas import settings_store
    from paas.config import settings
    from paas.db import connect, init_db
    from paas.modules.admin import service as admin_service

    settings.db_path = tmp_path / "mig.db"
    settings.data_dir = tmp_path / "data"
    settings.secret_key_path = tmp_path / "k"
    conn = connect()
    init_db(conn)
    settings_store.ensure_default_settings(conn)
    settings_store.set_setting(conn, "ai_mode", "ollama")
    settings_store.set_setting(conn, "ai_local_enabled", "")  # 模拟旧库无新字段
    conn.execute("DELETE FROM settings WHERE key='ai_local_enabled'")
    conn.commit()
    data = admin_service.get_ai_settings(conn)
    assert data["local_enabled"] is True
    assert data["order"] == "rules,local,cloud"
    conn.close()


async def test_migrate_old_ai_mode_with_defaults_present(tmp_path):
    """升级后 ensure_default_settings 已插入新默认字段，ai_mode 仍应被转换。"""
    from paas import settings_store
    from paas.config import settings
    from paas.db import connect, init_db
    from paas.modules.admin import service as admin_service

    settings.db_path = tmp_path / "mig2.db"
    settings.data_dir = tmp_path / "data"
    settings.secret_key_path = tmp_path / "k2"
    conn = connect()
    init_db(conn)
    settings_store.ensure_default_settings(conn)
    settings_store.set_setting(conn, "ai_mode", "ollama")
    # 不删除 ai_local_enabled（真实升级路径：默认值已存在）
    data = admin_service.get_ai_settings(conn)
    assert data["local_enabled"] is True
    assert data["order"] == "rules,local,cloud"
    rows = dict(conn.execute("SELECT key, value FROM settings").fetchall())
    assert "ai_mode" not in rows
    conn.close()


async def test_migrate_cloud_copies_old_key(tmp_path):
    from paas import settings_store
    from paas.config import settings
    from paas.db import connect, init_db
    from paas.modules.admin import service as admin_service
    from paas.security import encrypt_json

    settings.db_path = tmp_path / "mig3.db"
    settings.data_dir = tmp_path / "data"
    settings.secret_key_path = tmp_path / "k3"
    conn = connect()
    init_db(conn)
    settings_store.ensure_default_settings(conn)
    settings_store.set_setting(conn, "ai_mode", "cloud")
    settings_store.set_setting(conn, "ai_base_url", "https://api.example.com/v1")
    settings_store.set_setting(conn, "ai_model", "gpt-4o-mini")
    enc = encrypt_json({"key": "sk-test-123"})
    settings_store.set_setting(conn, "ai_api_key", enc)
    data = admin_service.get_ai_settings(conn)
    assert data["cloud_enabled"] is True
    assert data["order"] == "rules,cloud,local"
    assert data["cloud_base_url"] == "https://api.example.com/v1"
    assert data["cloud_model"] == "gpt-4o-mini"
    assert data["has_api_key"] is True
    stored = settings_store.get_setting(conn, "ai_cloud_api_key", "")
    from paas.security import decrypt_json

    assert decrypt_json(stored).get("key") == "sk-test-123"
    rows = dict(conn.execute("SELECT key, value FROM settings").fetchall())
    assert "ai_mode" not in rows
    conn.close()


async def test_migrate_off_cleans_orphan(tmp_path):
    from paas import settings_store
    from paas.config import settings
    from paas.db import connect, init_db
    from paas.modules.admin import service as admin_service

    settings.db_path = tmp_path / "mig4.db"
    settings.data_dir = tmp_path / "data"
    settings.secret_key_path = tmp_path / "k4"
    conn = connect()
    init_db(conn)
    settings_store.ensure_default_settings(conn)
    settings_store.set_setting(conn, "ai_mode", "off")
    data = admin_service.get_ai_settings(conn)
    assert data["local_enabled"] is False
    assert data["cloud_enabled"] is False
    rows = dict(conn.execute("SELECT key, value FROM settings").fetchall())
    assert "ai_mode" not in rows
    conn.close()


async def test_ollama_pull_success(monkeypatch):
    from paas.interpreter.core import ollama_pull

    class FakeResp:
        def raise_for_status(self):
            pass

        async def aread(self):
            return b"{}"

    class FakeCM:
        async def __aenter__(self):
            return FakeResp()

        async def __aexit__(self, *a):
            return False

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, *a, **k):
            return FakeCM()

    monkeypatch.setattr("paas.interpreter.core.httpx.AsyncClient", FakeClient)
    ok, msg = await ollama_pull("qwen2.5:0.5b", "http://localhost:11434")
    assert ok is True
    assert "已就绪" in msg


async def test_ollama_pull_connection_refused_message(monkeypatch):
    from paas.interpreter.core import ollama_pull

    class FakeCM:
        async def __aenter__(self):
            raise ConnectionRefusedError("[Errno 111] Connection refused")

        async def __aexit__(self, *a):
            return False

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, *a, **k):
            return FakeCM()

    monkeypatch.setattr("paas.interpreter.core.httpx.AsyncClient", FakeClient)
    ok, msg = await ollama_pull("qwen2.5:0.5b", "http://localhost:11434")
    assert ok is False
    assert "docker compose --profile ai up -d" in msg
