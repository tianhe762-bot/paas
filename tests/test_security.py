import pytest

from paas.config import settings
from paas.security import (
    decrypt_json,
    encrypt_json,
    hash_password,
    verify_password,
)


def test_password_hash_roundtrip():
    stored = hash_password("hunter2-secret")
    assert stored.startswith("scrypt$")
    assert verify_password("hunter2-secret", stored)
    assert not verify_password("wrong", stored)
    assert not verify_password("hunter2-secret", "garbage")


def test_encrypt_roundtrip():
    original_key = settings.secret_key
    try:
        settings.secret_key = "roundtrip-secret-abcdef"
        token = encrypt_json({"token": "abc123", "nested": {"a": 1}})
        assert token.startswith("enc:")
        assert decrypt_json(token) == {"token": "abc123", "nested": {"a": 1}}
        settings.secret_key = "different-key-xyz"
        with pytest.raises(Exception):
            decrypt_json(token)
    finally:
        settings.secret_key = original_key

