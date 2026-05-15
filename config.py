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

    # ── Required ───────────────────────────────────────────────────
    telegram_bot_token: str = Field(..., description="Telegram bot token")
    groq_api_key: str = Field(..., description="Groq API key (STT + LLM, free tier)")
    webapp_url: str = Field(..., description="Public HTTPS root of this app")

    # ── TTS providers (optional, fallback chain) ────────────────────
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = "pNInz6obpgDQGcFmaJgB"  # Adam — sharp male agent
    elevenlabs_model: str = "eleven_flash_v2_5"
    tts_provider: str = "auto"  # auto | elevenlabs | edge
    edge_tts_voice: str = "en-US-GuyNeural"

    # ── Models ─────────────────────────────────────────────────────
    groq_stt_model: str = "whisper-large-v3-turbo"
    groq_llm_model: str = "llama-3.3-70b-versatile"

    # ── Auth ───────────────────────────────────────────────────────
    allowed_user_ids: List[int] = Field(default_factory=list)

    # ── Server ─────────────────────────────────────────────────────
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

    def resolved_tts_provider(self) -> str:
        if self.tts_provider == "auto":
            return "elevenlabs" if self.elevenlabs_api_key else "edge"
        return self.tts_provider


settings = Settings()  # type: ignore[call-arg]
