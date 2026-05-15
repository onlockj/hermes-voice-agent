from __future__ import annotations

from typing import List

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    telegram_bot_token: str = Field(..., description="Telegram bot token from BotFather")
    openai_api_key: str = Field(..., description="OpenAI API key with Realtime access")
    webapp_url: str = Field(..., description="Public URL of this app (no trailing slash)")

    allowed_user_ids: List[int] = Field(default_factory=list)
    model: str = "gpt-4o-realtime-preview-2024-10-01"
    voice: str = "ash"

    host: str = "0.0.0.0"
    port: int = 8080

    @field_validator("allowed_user_ids", mode="before")
    @classmethod
    def _split_csv(cls, v):
        if v is None or v == "":
            return []
        if isinstance(v, str):
            return [int(x.strip()) for x in v.split(",") if x.strip()]
        return v

    @field_validator("webapp_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")


settings = Settings()  # type: ignore[call-arg]
