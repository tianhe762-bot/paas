from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "PAAS"

    # 入站 API 鉴权：Adapter / 外部机器人调用 /api/v1/message/inbound 时使用
    api_key: str = "dev-api-key-change-me"

    # 初始管理员账号（仅首次启动时写入数据库，之后以数据库为准）
    admin_username: str = "admin"
    admin_password: str = ""

    # 敏感信息加密密钥：留空则自动生成并保存到 data/secret.key
    secret_key: str = ""

    data_dir: Path = Path("data")
    logs_dir: Path = Path("logs")
    db_path: Path = Path("data/account.db")
    backup_dir: Path = Path("data/backups")
    secret_key_path: Path = Path("data/secret.key")

    tz: str = "Asia/Shanghai"

    # QQ 官方机器人
    qq_api_base: str = "https://api.bot.qq.com"
    qq_token_url: str = "https://bots.qq.com/app/getAppAccessToken"

    # Telegram
    tg_api_base: str = "https://api.telegram.org"

    http_timeout: float = 15.0
    session_ttl_hours: int = 12


settings = Settings()


def resolve_path(p: Path) -> Path:
    return p if p.is_absolute() else (Path.cwd() / p)

