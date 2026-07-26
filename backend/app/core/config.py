"""Single source of configuration.

Nothing else in the codebase reads ``os.environ`` directly - everything goes
through :func:`get_settings`.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Datastores -------------------------------------------------------
    database_url: str = "postgresql+asyncpg://agentops:agentops@localhost:5432/agentops"
    redis_url: str = "redis://localhost:6379/0"

    # --- LLM provider -----------------------------------------------------
    # `mock` is the only provider wired up today. The retry/backoff/timeout and
    # error-classification machinery in llm_client.py is provider-agnostic and
    # real; only the transport is mocked.
    llm_provider: Literal["mock"] = "mock"
    llm_model: str = "mock-small"
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_timeout_seconds: float = 30.0

    # --- Mock provider behaviour (drives the retry/failure demo) ----------
    mock_failure_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    mock_malformed_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    mock_latency_ms: int = Field(default=350, ge=0)
    mock_seed: int | None = None

    # --- Orchestrator policy (TECH_SPEC 3.2) ------------------------------
    max_retries: int = Field(default=3, ge=1)
    backoff_base_seconds: float = Field(default=1.0, ge=0.0)
    backoff_multiplier: float = Field(default=3.0, ge=1.0)
    max_rework_cycles: int = Field(default=2, ge=0)

    # --- Server -----------------------------------------------------------
    log_level: str = "INFO"
    cors_origins: str = "http://localhost:3000"

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, value: str) -> str:
        """A bare ``postgresql://`` URL silently selects the sync psycopg
        driver and then fails deep inside the first query. Catch it at boot."""
        if value.startswith("postgresql://"):
            raise ValueError(
                "DATABASE_URL must use the async driver: "
                "postgresql+asyncpg://... (got a bare postgresql:// URL)"
            )
        return value

    @field_validator("mock_seed", mode="before")
    @classmethod
    def _blank_seed_is_none(cls, value: object) -> object:
        # An empty env var arrives as "" and would fail int coercion.
        if value == "":
            return None
        return value

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def backoff_delay(self, attempt: int) -> float:
        """Delay before retry ``attempt`` (0-indexed): 1s, 3s, 9s by default."""
        return self.backoff_base_seconds * (self.backoff_multiplier**attempt)


@lru_cache
def get_settings() -> Settings:
    return Settings()
