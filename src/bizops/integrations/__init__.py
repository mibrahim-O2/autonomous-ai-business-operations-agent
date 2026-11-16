"""Business system adapters.

Agents never talk to a CRM or mail server directly. They call tools; tools call
an adapter interface; the adapter is either a mock (default) or a real
implementation configured by the operator.
"""

from .base import BillingAdapter, CalendarAdapter, CRMAdapter, EmailAdapter, IntegrationError, Integrations
from .mock import build_mock_integrations, seed_demo_data

__all__ = [
    "BillingAdapter",
    "CRMAdapter",
    "CalendarAdapter",
    "EmailAdapter",
    "IntegrationError",
    "Integrations",
    "build_mock_integrations",
    "seed_demo_data",
]
