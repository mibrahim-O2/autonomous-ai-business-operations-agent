"""Framework for real HTTP integrations.

`HttpIntegration` gives every real adapter the same reliability behaviour:
timeouts, retries with exponential backoff and jitter on transient failures, a
circuit breaker that stops calling a failing service, idempotency keys on
writes, and errors mapped to `IntegrationError` with a correct `retryable` flag.

`RestCRMAdapter` is a worked example that implements the CRM contract against a
conventional JSON REST API. It is tested against an in-memory transport; it has
not been pointed at a real vendor.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any

import httpx

from .base import IntegrationError

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class CircuitBreaker:
    """Opens after `threshold` consecutive failures; allows one trial call after `reset_seconds`."""

    def __init__(self, threshold: int = 5, reset_seconds: float = 30.0, clock=time.monotonic):
        self.threshold, self.reset_seconds, self._clock = threshold, reset_seconds, clock
        self.failures = 0
        self.opened_at: float | None = None

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        return "half_open" if self._clock() - self.opened_at >= self.reset_seconds else "open"

    def before_call(self) -> None:
        if self.state == "open":
            raise IntegrationError("circuit breaker is open; service is being given time to recover", retryable=True)

    def record(self, ok: bool) -> None:
        if ok:
            self.failures, self.opened_at = 0, None
            return
        self.failures += 1
        if self.failures >= self.threshold or self.state == "half_open":
            self.opened_at = self._clock()


class HttpIntegration:
    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: float = 10.0,
        max_attempts: int = 3,
        backoff_base: float = 0.2,
        transport: httpx.AsyncBaseTransport | None = None,
        breaker: CircuitBreaker | None = None,
        sleep=asyncio.sleep,
    ):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout, transport=transport)
        self.max_attempts, self.backoff_base = max_attempts, backoff_base
        self.breaker = breaker or CircuitBreaker()
        self._sleep = sleep

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        """Send a request with retries. Raises IntegrationError; never returns an error response."""
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        last_error = "no attempt made"
        for attempt in range(1, self.max_attempts + 1):
            self.breaker.before_call()
            try:
                response = await self._client.request(method, path, json=json, params=params, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error, retryable = f"{type(exc).__name__}: {exc}", True
            else:
                if response.status_code < 400:
                    self.breaker.record(True)
                    return response.json() if response.content else None
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                retryable = response.status_code in RETRYABLE_STATUS
                if not retryable:
                    # A client error is our mistake, not a service fault: do not trip the breaker.
                    raise IntegrationError(last_error, retryable=False)
            self.breaker.record(False)
            if attempt < self.max_attempts:
                # Exponential backoff with jitter so that many clients do not retry in step.
                await self._sleep(self.backoff_base * 2 ** (attempt - 1) * (0.5 + random.random() / 2))
        raise IntegrationError(
            f"{method} {path} failed after {self.max_attempts} attempts: {last_error}", retryable=True
        )


class RestCRMAdapter:
    """Example real adapter: the CRM contract over a JSON REST API.

    Expected endpoints: GET /contacts/{email}, GET /contacts?q=, PUT /contacts/{email},
    POST /contacts/{email}/notes, POST /contacts/{email}/merge. Adjust paths and field
    names for a specific vendor; see REAL-INTEGRATIONS-SETUP.md.

    The adapter contract is synchronous, so these methods are the async building
    blocks; `SyncBridge` in a real deployment (or an async tool) would call them.
    """

    def __init__(self, http: HttpIntegration):
        self.http = http

    def _path(self, tenant_id: str, email: str = "") -> str:
        return f"/tenants/{tenant_id}/contacts" + (f"/{email.lower()}" if email else "")

    async def find_contact(self, tenant_id: str, email: str) -> dict[str, Any] | None:
        try:
            return await self.http.request("GET", self._path(tenant_id, email))
        except IntegrationError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise

    async def search_contacts(self, tenant_id: str, query: str, limit: int = 10) -> list[dict[str, Any]]:
        return await self.http.request("GET", self._path(tenant_id), params={"q": query, "limit": limit})

    async def upsert_contact(
        self, tenant_id: str, email: str, fields: dict[str, Any], idempotency_key: str | None = None
    ) -> dict[str, Any]:
        return await self.http.request(
            "PUT", self._path(tenant_id, email), json=fields, idempotency_key=idempotency_key
        )

    async def add_note(
        self, tenant_id: str, email: str, note: str, author: str, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        return await self.http.request(
            "POST",
            self._path(tenant_id, email) + "/notes",
            json={"text": note, "by": author},
            idempotency_key=idempotency_key,
        )
