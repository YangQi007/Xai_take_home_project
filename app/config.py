"""Application configuration using Pydantic Settings."""
from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Application
    app_name: str = "X Conversation Analytics"
    debug: bool = False

    # Database
    database_url: str = "postgresql+asyncpg://user:pass@localhost:5432/conversations"
    db_pool_size: int = 10
    db_max_overflow: int = 20

    # Grok API
    xai_api_key: str
    grok_api_base_url: str = "https://api.x.ai/v1"
    grok_model: str = "grok-3-mini-fast"
    grok_timeout: float = 30.0
    grok_max_retries: int = 3

    # Rate limiting
    rate_limit_requests_per_minute: int = 100
    rate_limit_burst: int = 20

    # Processing
    worker_count: int = 5
    queue_max_size: int = 1000
    batch_size_min: int = 1
    batch_size_max: int = 50
    batch_size_initial: int = 10

    # Circuit breaker
    circuit_breaker_failure_threshold: int = 5
    circuit_breaker_recovery_timeout: float = 30.0
    circuit_breaker_half_open_requests: int = 3

    # Cache
    cache_ttl_hours: int = 24

    # Cost estimation (per 1K tokens)
    grok_input_cost_per_1k: float = 0.0001
    grok_output_cost_per_1k: float = 0.0004


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
