"""Runtime configuration, read from environment variables and an optional .env file.

Every tunable limit lives here so that behaviour can be changed per deployment
without touching code. Secrets are read from the environment only.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="BIZOPS_", extra="ignore")

    data_dir: Path = Path("var")
    default_tenant: str = "demo"

    llm_provider: str = "scripted"
    llm_model: str = ""
    # Provider keys keep their conventional names, so they bypass the BIZOPS_ prefix.
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    openai_api_key: SecretStr | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    openai_base_url: str | None = Field(default=None, validation_alias="OPENAI_BASE_URL")

    max_steps: int = Field(default=20, ge=1, le=200)
    max_replans: int = Field(default=2, ge=0, le=10)
    max_run_cost_usd: float = Field(default=1.0, ge=0)
    context_token_budget: int = Field(default=6000, ge=500)

    # Load the fictional business records and policy documents for the default tenant at start-up.
    seed_demo_data: bool = True
    # Shared secrets. Unset means the corresponding HTTP surface is disabled, never open.
    api_key: SecretStr | None = None
    webhook_secret: SecretStr | None = None

    log_format: str = "text"
    log_level: str = "INFO"

    @property
    def database_path(self) -> Path:
        return self.data_dir / "bizops.db"


@lru_cache
def get_settings() -> Settings:
    return Settings()
