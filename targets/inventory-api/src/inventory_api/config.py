from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "sqlite:///./inventory.db"
    jwt_secret_key: str = "development-only-change-me-use-a-longer-key"
    access_token_expire_minutes: int = 60
    admin_email: str | None = None
    admin_password: str | None = None

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    return Settings()
