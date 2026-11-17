"""Retry policy and error classification for hosted model providers.

`classify_provider_error` turns a vendor SDK exception into an `LLMError` that
says whether another attempt may help. `ResilientLLM` wraps a provider and
retries the retryable ones with exponential backoff and jitter, honouring the
provider's Retry-After hint. It never retries authentication or quota errors,
which cannot succeed, and it never includes credentials in a message.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import Any

from ..core.errors import LLMError
from .base import LLMMessage, LLMProvider, LLMResponse, ToolSchema

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}
RETRYABLE_NAMES = {"APITimeoutError", "APIConnectionError", "InternalServerError", "RateLimitError", "OverloadedError"}


def _retry_after(exc: BaseException) -> float | None:
    headers = getattr(getattr(exc, "response", None), "headers", None) or {}
    for name in ("retry-after-ms", "retry-after"):
        value = headers.get(name)
        if value is None:
            continue
        try:
            seconds = float(value) / (1000 if name.endswith("ms") else 1)
        except (TypeError, ValueError):
            continue  # an HTTP-date form is rare here; fall back to computed backoff
        if seconds >= 0:
            return seconds
    return None


def classify_provider_error(exc: BaseException, provider: str) -> LLMError:
    """Map a vendor exception to LLMError. Only the status, type, and error code are kept."""
    if isinstance(exc, LLMError):
        return exc
    status = getattr(exc, "status_code", None)
    name = type(exc).__name__
    body = getattr(exc, "body", None)
    code = getattr(exc, "code", None) or (
        (body.get("error") or {}).get("code")
        if isinstance(body, dict) and isinstance(body.get("error"), dict)
        else None
    )
    if status in (401, 403) or name in ("AuthenticationError", "PermissionDeniedError"):
        return LLMError(f"{provider}: authentication failed; check the provider API key and its permissions")
    if code == "insufficient_quota":
        return LLMError(f"{provider}: the account has no remaining quota; check billing with the provider")
    if status == 404 or name == "NotFoundError":
        return LLMError(f"{provider}: model or endpoint not found; check BIZOPS_LLM_MODEL")
    if status == 400 or name == "BadRequestError":
        return LLMError(f"{provider}: request rejected as invalid ({code or 'bad_request'})")
    if status in RETRYABLE_STATUS or name in RETRYABLE_NAMES or isinstance(exc, TimeoutError | ConnectionError):
        kind = "rate limited" if status == 429 or name == "RateLimitError" else "temporarily unavailable"
        return LLMError(f"{provider}: {kind} ({status or name})", retryable=True, retry_after=_retry_after(exc))
    return LLMError(f"{provider}: unexpected {name}" + (f" (status {status})" if status else ""))


class ResilientLLM:
    """Wraps a provider with bounded retries. Agents see either a response or a final LLMError."""

    def __init__(
        self,
        inner: LLMProvider,
        max_retries: int = 4,
        max_delay_seconds: float = 30.0,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ):
        self.inner, self.max_retries, self.max_delay_seconds, self._sleep = inner, max_retries, max_delay_seconds, sleep
        self.stats = {"calls": 0, "retries": 0, "rate_limited": 0, "failures": 0}

    @property
    def name(self) -> str:
        return self.inner.name

    @property
    def model(self) -> str:
        return self.inner.model

    async def complete(self, messages: list[LLMMessage], tools: list[ToolSchema] | None = None) -> LLMResponse:
        self.stats["calls"] += 1
        attempt = 0
        while True:
            try:
                return await self.inner.complete(messages, tools)
            except Exception as exc:
                error = classify_provider_error(exc, self.inner.name)
                if "rate limited" in str(error):
                    self.stats["rate_limited"] += 1
                if not error.retryable or attempt >= self.max_retries:
                    self.stats["failures"] += 1
                    raise error from exc
                backoff = min(self.max_delay_seconds, 2.0**attempt)
                delay = error.retry_after if error.retry_after is not None else random.uniform(backoff / 2, backoff)
                attempt += 1
                self.stats["retries"] += 1
                await self._sleep(min(delay, self.max_delay_seconds))
