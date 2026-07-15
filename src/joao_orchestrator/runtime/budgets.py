"""Resource budgets (V0.2 will wire enforcement into the convergence loop).

V1.4.1 defines the data model so profiles and tasks can carry limits; the
actual attempt/runtime/cost gating arrives with V0.2's bounded convergence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TaskBudget:
    """Per-task resource envelope. Defaults are conservative."""
    max_coder_attempts: int = 2
    max_validation_attempts: int = 3
    max_total_runtime_seconds: int = 1800
    max_total_provider_calls: int = 10
    max_total_estimated_cost: int = 1.0          # abstract units
    retryable_failure_types: list = field(default_factory=lambda: [
        "test_failure", "lint_warning", "transient_error"])
    non_retryable_failure_types: list = field(default_factory=lambda: [
        "policy_violation", "forbidden_path_change", "syntax_error_in_request"])

    def to_dict(self) -> dict:
        return {
            "max_coder_attempts": self.max_coder_attempts,
            "max_validation_attempts": self.max_validation_attempts,
            "max_total_runtime_seconds": self.max_total_runtime_seconds,
            "max_total_provider_calls": self.max_total_provider_calls,
            "max_total_estimated_cost": self.max_total_estimated_cost,
            "retryable_failure_types": list(self.retryable_failure_types),
            "non_retryable_failure_types": list(self.non_retryable_failure_types),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TaskBudget":
        return cls(
            max_coder_attempts=int(d.get("max_coder_attempts", 2)),
            max_validation_attempts=int(d.get("max_validation_attempts", 3)),
            max_total_runtime_seconds=int(d.get("max_total_runtime_seconds", 1800)),
            max_total_provider_calls=int(d.get("max_total_provider_calls", 10)),
            max_total_estimated_cost=float(d.get("max_total_estimated_cost", 1.0)),
            retryable_failure_types=list(d.get("retryable_failure_types", [])),
            non_retryable_failure_types=list(d.get("non_retryable_failure_types", [])),
        )


def is_retryable(failure_type: str, budget: TaskBudget) -> bool:
    """A failure is retryable only if explicitly listed as retryable AND not
    listed as non-retryable. Non-retryable wins."""
    if failure_type in budget.non_retryable_failure_types:
        return False
    return failure_type in budget.retryable_failure_types
