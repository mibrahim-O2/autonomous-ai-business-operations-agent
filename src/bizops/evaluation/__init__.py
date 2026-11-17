"""Evaluation: scenario cases with checks, a retrieval quality set, a runner, and stored results."""

from .cases import CASES, RETRIEVAL_SET, EvalCase
from .runner import EvaluationStore, run_evaluation

__all__ = ["CASES", "RETRIEVAL_SET", "EvalCase", "EvaluationStore", "run_evaluation"]
