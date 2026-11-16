"""Build the configured provider."""

from __future__ import annotations

from ..core.config import Settings
from ..core.errors import ConfigurationError
from .base import LLMProvider
from .scripted import ScriptedLLM


def build_llm(settings: Settings) -> LLMProvider:
    provider = settings.llm_provider.lower()
    if provider == "scripted":
        return ScriptedLLM()
    if provider == "anthropic":
        if not settings.anthropic_api_key:
            raise ConfigurationError("ANTHROPIC_API_KEY is required for the anthropic provider")
        from .providers import AnthropicProvider

        return AnthropicProvider(
            settings.anthropic_api_key.get_secret_value(), settings.llm_model or "claude-sonnet-5-5"
        )
    if provider == "openai":
        if not settings.openai_api_key:
            raise ConfigurationError("OPENAI_API_KEY is required for the openai provider")
        if not settings.llm_model:
            raise ConfigurationError("BIZOPS_LLM_MODEL is required for the openai provider")
        from .providers import OpenAICompatibleProvider

        return OpenAICompatibleProvider(
            settings.openai_api_key.get_secret_value(), settings.llm_model, settings.openai_base_url
        )
    raise ConfigurationError(f"Unknown LLM provider '{settings.llm_provider}'")
