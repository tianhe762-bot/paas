import os

import pytest
from httpx import ASGITransport, AsyncClient

# 注意：pydantic-settings 默认无前缀，环境变量名与字段名对应（大小写不敏感）
os.environ["API_KEY"] = "test-api-key"
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD"] = "test-admin-pass-123"
os.environ["SECRET_KEY"] = "test-secret-key-0123456789abcdef"


@pytest.fixture()
def conn(tmp_path):
    from paas.config import settings
    from paas.db import connect, init_db

    settings.data_dir = tmp_path / "data"
    settings.db_path = tmp_path / "test.db"
    settings.secret_key_path = tmp_path / "secret.key"
    db_conn = connect()
    init_db(db_conn)
    yield db_conn
    db_conn.close()


def _prepare_app(db_path):
    from paas import settings_store
    from paas.communication.registry import AdapterManager
    from paas.config import settings
    from paas.db import connect, init_db
    from paas.modules.admin import service as admin_service
    from paas.router import Router

    settings.db_path = db_path
    settings.data_dir = db_path.parent / "data"
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
