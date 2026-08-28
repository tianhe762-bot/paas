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

    async def fake_ai(conn_, content, backend):
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

    async def fake_ai(conn_, content, backend):
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

    async def fake_ai(conn_, content, backend):
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

    async def fake_ai(conn_, content, backend):
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
