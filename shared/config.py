"""Application configuration with startup validation.

All config is validated at import time via pydantic-settings.
Missing required values cause an immediate, clear error.
In live mode, vendor access tokens are required.
"""

from pydantic import model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_prefix": "SH_", "env_file": ".env"}

    # Database
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/sleep_harmonizer"

    # Adapter mode: "fixture" or "live"
    adapter_mode: str = "fixture"

    # Oura
    oura_base_url: str = "https://api.ouraring.com"
    oura_access_token: str = ""

    # Withings
    withings_base_url: str = "https://wbsapi.withings.net"
    withings_access_token: str = ""

    # API
    api_version: str = "v1"
    default_page_limit: int = 25
    max_page_limit: int = 100

    # Idempotency
    idempotency_key_ttl_hours: int = 24

    # Retry
    retry_max_attempts: int = 3
    retry_max_wait_seconds: int = 30

    @model_validator(mode="after")
    def validate_live_mode_secrets(self) -> "Settings":
        """Fail fast at startup if live mode is selected but credentials are missing."""
        if self.adapter_mode == "live":
            missing = []
            if not self.oura_access_token:
                missing.append("SH_OURA_ACCESS_TOKEN")
            if not self.withings_access_token:
                missing.append("SH_WITHINGS_ACCESS_TOKEN")
            if missing:
                raise ValueError(
                    f"adapter_mode='live' requires vendor credentials. "
                    f"Missing: {', '.join(missing)}"
                )
        return self


settings = Settings()
