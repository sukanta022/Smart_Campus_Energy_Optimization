"""Application configuration loaded from environment variables."""
from __future__ import annotations

import os

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Override with env vars or a `.env` file."""

    model_config = SettingsConfigDict(
        env_file=os.environ.get("ENV_FILE", ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Service
    app_name: str = Field(default="gridwise-llm")
    log_level: str = Field(default="INFO")
    request_timeout_seconds: float = Field(default=8.0)

    # OpenAI
    openai_api_key: str = Field(default_factory=lambda: os.environ.get("OPENAI_API_KEY", ""))
    openai_model: str = Field(default="gpt-4o-mini")
    openai_timeout_seconds: float = Field(default=3.0)
    openai_seed: int = Field(default=42)
    openai_max_retries: int = Field(default=2)

    # Optimizer
    optimizer_time_limit_seconds: float = Field(default=3.0)
    optimizer_warm_start: bool = Field(default=True)

    # Resilience
    enable_fallback_parser: bool = Field(default=True)
    redis_url: str | None = Field(default_factory=lambda: os.environ.get("REDIS_URL") or None)
    rate_limit_per_minute: int = Field(default=600)
    circuit_breaker_fail_max: int = Field(default=5)
    circuit_breaker_reset_timeout: int = Field(default=30)

    # Health
    health_check_redis: bool = Field(default=False)

    def openai_configured(self) -> bool:
        return bool(self.openai_api_key)


_settings: Settings | None = None


def get_settings() -> Settings:
    """Cached settings accessor."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
