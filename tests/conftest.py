import os

import pytest

# 注意：pydantic-settings 默认无前缀，环境变量名与字段名对应（大小写不敏感）
os.environ["API_KEY"] = "test-api-key"
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD"] = "test-admin-pass-123"
os.environ["SECRET_KEY"] = "test-secret-key-0123456789abcdef"


@pytest.fixture()
def conn(tmp_path):
    from paas.config import settings
    from paas.db import connect, init_db

    settings.db_path = tmp_path / "test.db"
    settings.secret_key_path = tmp_path / "secret.key"
    db_conn = connect()
    init_db(db_conn)
    yield db_conn
    db_conn.close()
