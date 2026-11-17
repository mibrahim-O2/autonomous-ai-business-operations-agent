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


class StaleLease(BizOpsError):
    """This worker no longer owns the run: another worker claimed it with a newer generation.

    A worker that sees this must stop at once without writing anything further.
    """


class AuthenticationError(BizOpsError):
    """The caller did not present a valid credential."""


class Conflict(BizOpsError):
    """The request is valid but the object is not in a state that allows it (for example, already decided)."""


class LLMError(BizOpsError):
    """A model call failed. `retryable` says whether trying again may help; `retry_after` is a provider hint."""

    def __init__(self, message: str, *, retryable: bool = False, retry_after: float | None = None):
        super().__init__(message)
        self.retryable, self.retry_after = retryable, retry_after
