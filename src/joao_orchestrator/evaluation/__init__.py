"""Deterministic local evaluation harness for JOSS (C1).

Provider-free, network-free keep-if-better evaluation. Runs deterministic
commands for a baseline and a candidate subject, compares a fixed set of
metrics, and yields an integrity-signed KEEP / REJECT / TIE verdict.

The package reuses canonical orchestrator primitives for durable persistence
(:mod:`joao_orchestrator.storage.atomic`) and the secret-redaction contract.
Standard library only; no provider, no network, no package installation.
"""

from .comparator import ComparisonError, EvaluationComparator
from .models import (
    CommandResult,
    CommandSpec,
    EvaluationReport,
    EvaluationSpec,
    KeepDecision,
    KeepVerdict,
    MetricDirection,
    MetricResult,
    MetricRule,
    TiePolicy,
)
from .runner import EvaluationError, EvaluationRunner, sanitize_environment

__all__ = [
    "CommandResult",
    "CommandSpec",
    "ComparisonError",
    "EvaluationComparator",
    "EvaluationError",
    "EvaluationReport",
    "EvaluationRunner",
    "EvaluationSpec",
    "KeepDecision",
    "KeepVerdict",
    "MetricDirection",
    "MetricResult",
    "MetricRule",
    "TiePolicy",
    "sanitize_environment",
]
