import base64
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from paas.config import settings
from paas.log import get_logger

log = get_logger("paas.security")

SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32


def hash_password(password: str) -> str:
    """Return scrypt hash string: scrypt$n$r$p$salt_b64$hash_b64."""
    salt = os.urandom(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
    )
    return "$".join(
        [
            "scrypt",
            str(SCRYPT_N),
            str(SCRYPT_R),
            str(SCRYPT_P),
            base64.b64encode(salt).decode(),
            base64.b64encode(digest).decode(),
        ]
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n_s, r_s, p_s, salt_b64, hash_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n_s),
            r=int(r_s),
            p=int(p_s),
            dklen=len(expected),
        )
        return hmac.compare_digest(digest, expected)
    except Exception:
        return False


def _load_or_create_key() -> bytes:
    env_key = settings.secret_key.strip()
    if env_key:
        return base64.urlsafe_b64encode(hashlib.sha256(env_key.encode()).digest())

    key_path = Path(settings.secret_key_path)
    if not key_path.is_absolute():
        key_path = Path.cwd() / key_path
    key_path.parent.mkdir(parents=True, exist_ok=True)
    if key_path.exists():
        return key_path.read_bytes().strip()
    key = Fernet.generate_key()
    key_path.write_bytes(key)
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    log.warning("SECRET_KEY 未设置，已自动生成并保存到 %s（请勿删除，否则已存凭证无法解密）", key_path)
    return key


def get_fernet() -> Fernet:
    return Fernet(_load_or_create_key())


def encrypt_json(obj: Any) -> str:
    return "enc:" + get_fernet().encrypt(json.dumps(obj, ensure_ascii=False).encode("utf-8")).decode()


def decrypt_json(token: str) -> Any:
    if not token.startswith("enc:"):
        return json.loads(token)  # 兼容旧明文（不推荐）
    try:
        raw = get_fernet().decrypt(token[4:].encode())
    except InvalidToken:
        log.error("解密失败：SECRET_KEY 与加密时不一致，或 data/secret.key 已丢失")
        raise
    return json.loads(raw.decode("utf-8"))


def new_session_token() -> str:
    return secrets.token_urlsafe(32)
