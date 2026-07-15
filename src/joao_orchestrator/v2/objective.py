"""V2 objective contract + backward planner (§10).

Before editing anything, V2 writes an *objective contract* — a backward-planned
artifact that starts from the user-visible product outcome and chains down to
the nearest unsatisfied product gate. The contract pins scope, budgets, stop
conditions, fallback, and rollback. It is the single artifact that prevents
"generic JOSS work took priority over open product gates" (failure #15) and
"planning without diff" (loop pattern, §14).

Lives at ``runs/<run_id>/objective_contract.json`` (outside Git).

Example backward chain (§10)::

    daily email
    ← scheduler executes
    ← SMTP works
    ← public URL reachable
    ← publication succeeds
    ← validation passes
    ← report generated
    ← live data updated
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..storage.atomic import atomic_write_json  # reuse
from . import V2_SCHEMA_VERSION
from .state import JOSS_ROOT

RUNS_ROOT = JOSS_ROOT / "runs"


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Budgets (§13) — defaults taken verbatim from the master prompt.
# ---------------------------------------------------------------------------

@dataclass
class RunBudgets:
    exploration_seconds: int = 900
    max_repair_loops_per_gate: int = 2
    max_same_file_reads_unchanged: int = 2
    max_same_failing_command_without_new_evidence: int = 2
    max_repeated_summary: int = 1
    max_active_objectives: int = 1
    max_full_suite_runs: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def defaults(cls) -> "RunBudgets":
        return cls()


# ---------------------------------------------------------------------------
# Objective contract
# ---------------------------------------------------------------------------

@dataclass
class ObjectiveContract:
    """The pinned, backward-planned contract for one objective."""
    requested_user_outcome: str
    visible_acceptance_artifact: str
    single_objective: str
    in_scope: list[str] = field(default_factory=list)
    out_of_scope: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    backward_chain: list[str] = field(default_factory=list)  # goal → root
    ordered_gates: list[str] = field(default_factory=list)
    known_good_paths: dict[str, str] = field(default_factory=dict)
    budgets: RunBudgets = field(default_factory=RunBudgets.defaults)
    stop_conditions: list[str] = field(default_factory=list)
    fallback: str = ""
    rollback: str = ""
    acceptance_checks: list[str] = field(default_factory=list)
    project_id: str = ""
    run_id: str = ""
    baseline_head: str = ""
    created_at: str = ""

    def validate(self) -> None:
        """Fail-closed validation: the contract is unusable without these."""
        if not self.requested_user_outcome.strip():
            raise ValueError("objective contract: requested_user_outcome required")
        if not self.visible_acceptance_artifact.strip():
            raise ValueError(
                "objective contract: visible_acceptance_artifact required "
                "(a gate is not a product outcome)")
        if not self.single_objective.strip():
            raise ValueError("objective contract: single_objective required")
        if self.budgets.max_active_objectives != 1:
            raise ValueError(
                "objective contract: exactly one active objective allowed (§13)")
        # Backward chain must START from the user-visible outcome (§10).
        if self.backward_chain:
            first = self.backward_chain[0]
            if (self.requested_user_outcome
                    and self.requested_user_outcome not in first
                    and first not in self.requested_user_outcome):
                raise ValueError(
                    "objective contract: backward_chain must start from the "
                    "requested user outcome")

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["schema_version"] = V2_SCHEMA_VERSION
        return d


# ---------------------------------------------------------------------------
# Backward planner
# ---------------------------------------------------------------------------

class ObjectivePlanner:
    """Builds an :class:`ObjectiveContract` from a goal via backward planning."""

    def __init__(self, project_id: str, run_id: str = ""):
        self.project_id = project_id
        self.run_id = run_id

    def plan_backward(
        self, *, requested_user_outcome: str,
        visible_acceptance_artifact: str,
        backward_chain: Sequence[str], single_objective: str,
        in_scope: Sequence[str] = (), out_of_scope: Sequence[str] = (),
        dependencies: Sequence[str] = (), ordered_gates: Sequence[str] = (),
        known_good_paths: Mapping[str, str] | None = None,
        budgets: RunBudgets | None = None,
        stop_conditions: Sequence[str] = (),
        fallback: str = "", rollback: str = "",
        acceptance_checks: Sequence[str] = (),
        baseline_head: str = "",
    ) -> ObjectiveContract:
        """Create and validate a backward-planned contract.

        ``backward_chain`` is given goal-first (the user-visible outcome at
        index 0) down to the nearest unsatisfied product gate at the end.
        """
        contract = ObjectiveContract(
            requested_user_outcome=requested_user_outcome,
            visible_acceptance_artifact=visible_acceptance_artifact,
            single_objective=single_objective,
            in_scope=list(in_scope), out_of_scope=list(out_of_scope),
            dependencies=list(dependencies),
            backward_chain=list(backward_chain),
            ordered_gates=list(ordered_gates),
            known_good_paths=dict(known_good_paths or {}),
            budgets=budgets or RunBudgets.defaults(),
            stop_conditions=list(stop_conditions),
            fallback=fallback, rollback=rollback,
            acceptance_checks=list(acceptance_checks),
            project_id=self.project_id, run_id=self.run_id,
            baseline_head=baseline_head, created_at=_utcnow(),
        )
        contract.validate()
        return contract

    def save(self, contract: ObjectiveContract) -> Path:
        """Persist the contract to ``runs/<run_id>/objective_contract.json``."""
        d = RUNS_ROOT / (contract.run_id or self.run_id or "default")
        d.mkdir(parents=True, exist_ok=True)
        path = d / "objective_contract.json"
        atomic_write_json(path, contract.to_dict())
        return path

    @staticmethod
    def load(path: Path) -> ObjectiveContract:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        budgets = RunBudgets(**{k: data["budgets"][k]
                                for k in RunBudgets.__dataclass_fields__})
        return ObjectiveContract(
            requested_user_outcome=data["requested_user_outcome"],
            visible_acceptance_artifact=data["visible_acceptance_artifact"],
            single_objective=data["single_objective"],
            in_scope=data.get("in_scope", []),
            out_of_scope=data.get("out_of_scope", []),
            dependencies=data.get("dependencies", []),
            backward_chain=data.get("backward_chain", []),
            ordered_gates=data.get("ordered_gates", []),
            known_good_paths=data.get("known_good_paths", {}),
            budgets=budgets,
            stop_conditions=data.get("stop_conditions", []),
            fallback=data.get("fallback", ""),
            rollback=data.get("rollback", ""),
            acceptance_checks=data.get("acceptance_checks", []),
            project_id=data.get("project_id", ""),
            run_id=data.get("run_id", ""),
            baseline_head=data.get("baseline_head", ""),
            created_at=data.get("created_at", ""),
        )


__all__ = [
    "RunBudgets", "ObjectiveContract", "ObjectivePlanner", "RUNS_ROOT",
]
