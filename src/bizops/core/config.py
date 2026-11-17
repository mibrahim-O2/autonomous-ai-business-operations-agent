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
    # An empty variable (as in a freshly copied .env.example) means "not set", not an empty value.
    model_config = SettingsConfigDict(env_file=".env", env_prefix="BIZOPS_", extra="ignore", env_ignore_empty=True)

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
    # How long a worker's claim on a run lasts without renewal. A worker renews at each
    # checkpoint and from a heartbeat; another worker may take over only after expiry.
    run_lease_seconds: float = Field(default=60.0, gt=0)

    # Reliability limits.
    step_timeout_seconds: float = Field(default=120.0, gt=0)  # one specialist working on one step
    tool_timeout_seconds: float = Field(default=30.0, gt=0)  # one tool call
    retry_base_seconds: float = Field(default=0.5, ge=0)  # first delay before a retried step; doubles each time
    retry_max_seconds: float = Field(default=8.0, ge=0)
    llm_timeout_seconds: float = Field(default=60.0, gt=0)  # one request to a hosted model
    # Prices for the selected hosted model, in USD per million tokens. If unset, run cost is a rough
    # estimate from a generic rate; set both from the provider's current price list for real figures.
    llm_price_input_per_mtok: float | None = Field(default=None, ge=0)
    llm_price_output_per_mtok: float | None = Field(default=None, ge=0)
    llm_max_retries: int = Field(default=4, ge=0, le=10)  # rate limits and transient provider errors
    llm_retry_max_seconds: float = Field(default=30.0, ge=0)
    # A request for human approval that nobody answers within this time is treated as refused.
    approval_ttl_seconds: float = Field(default=86_400.0, gt=0)

    # Load the fictional business records and policy documents for the default tenant at start-up.
    seed_demo_data: bool = True
    # Shared secrets. Unset means the corresponding HTTP surface is disabled, never open.
    # BIZOPS_API_KEY is a bootstrap administrator key for the default tenant. Further keys, each
    # bound to one tenant and one role, are created with `bizops keys create`.
    api_key: SecretStr | None = None
    # BIZOPS_WEBHOOK_SECRET signs webhooks for the default tenant. BIZOPS_WEBHOOK_SECRETS binds more
    # senders to tenants: JSON such as {"shop": {"tenant": "acme", "secret": "..."}}, keyed by the
    # source name in the webhook URL.
    webhook_secret: SecretStr | None = None
    webhook_secrets: SecretStr | None = None

    log_format: str = "text"
    log_level: str = "INFO"

    @property
    def database_path(self) -> Path:
        return self.data_dir / "bizops.db"


@lru_cache
def get_settings() -> Settings:
    return Settings()
