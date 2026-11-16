"""Exception hierarchy. Callers catch BizOpsError; subclasses say what kind of failure it was."""

from __future__ import annotations


class BizOpsError(Exception):
    """Base class for every error raised deliberately by this package."""


class ConfigurationError(BizOpsError):
    """The deployment is misconfigured (unknown provider, missing key)."""


class PlanningError(BizOpsError):
    """The planner could not produce a valid plan."""


class ToolError(BizOpsError):
    """A tool failed. `retryable` tells the executor whether another attempt may help."""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class PermissionDenied(BizOpsError):
    """An agent or user tried something its permissions do not allow."""


class BudgetExceeded(BizOpsError):
    """A run hit its step, replan, or cost limit."""


class RunNotFound(BizOpsError):
    """No run with the requested id exists for the tenant."""
