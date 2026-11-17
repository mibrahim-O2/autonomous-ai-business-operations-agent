"""Build the configured provider."""

from __future__ import annotations

from ..core.config import Settings
from ..core.errors import ConfigurationError
from .base import PRICE_TABLE, LLMProvider
from .resilient import ResilientLLM
from .scripted import ScriptedLLM


def build_llm(settings: Settings) -> LLMProvider:
    """Build the provider named by BIZOPS_LLM_PROVIDER. Hosted providers are wrapped with retries."""
    provider = settings.llm_provider.lower()
    if provider == "scripted":
        return ScriptedLLM()
    if settings.llm_model and settings.llm_price_input_per_mtok is not None:
        # Cost tracking uses the prices the operator supplies for the selected model.
        PRICE_TABLE[settings.llm_model] = (
            settings.llm_price_input_per_mtok,
            settings.llm_price_output_per_mtok or 0.0,
        )

    def resilient(inner: LLMProvider) -> LLMProvider:
        return ResilientLLM(inner, settings.llm_max_retries, settings.llm_retry_max_seconds)

    if provider == "anthropic":
        if not settings.anthropic_api_key:
            raise ConfigurationError("ANTHROPIC_API_KEY is required for the anthropic provider")
        from .providers import AnthropicProvider

        return resilient(
            AnthropicProvider(
                settings.anthropic_api_key.get_secret_value(),
                settings.llm_model or "claude-sonnet-5-5",
                timeout=settings.llm_timeout_seconds,
            )
        )
    if provider == "openai":
        if not settings.openai_api_key:
            raise ConfigurationError("OPENAI_API_KEY is required for the openai provider")
        if not settings.llm_model:
            raise ConfigurationError("BIZOPS_LLM_MODEL is required for the openai provider")
        from .providers import OpenAICompatibleProvider

        return resilient(
            OpenAICompatibleProvider(
                settings.openai_api_key.get_secret_value(),
                settings.llm_model,
                settings.openai_base_url,
                timeout=settings.llm_timeout_seconds,
            )
        )
    raise ConfigurationError(f"Unknown LLM provider '{settings.llm_provider}'")
