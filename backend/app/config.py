from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    database_url: str = Field(
        default="postgresql+asyncpg://pathway:pathway@localhost:5432/pathway",
        alias="DATABASE_URL",
    )

    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    # Phase 3 rename: USDA_API_KEY → USDA_FDC_API_KEY. Kept as alias so existing
    # .env files don't break.
    usda_fdc_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("USDA_FDC_API_KEY", "USDA_API_KEY"),
    )
    usda_ams_api_key: str = Field(default="", alias="USDA_AMS_API_KEY")
    google_places_api_key: str = Field(default="", alias="GOOGLE_PLACES_API_KEY")
    resend_api_key: str = Field(default="", alias="RESEND_API_KEY")
    resend_from_email: str = Field(default="", alias="RESEND_FROM_EMAIL")
    imap_host: str = Field(default="", alias="IMAP_HOST")
    imap_user: str = Field(default="", alias="IMAP_USER")
    imap_password: str = Field(default="", alias="IMAP_PASSWORD")

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    env: Literal["dev", "prod", "test"] = Field(default="dev", alias="ENV")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
