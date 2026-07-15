"""C3 Roadmap Compiler — content-addressed data models.

A Roadmap is a frozen, hashable, content-addressed artifact that binds a set of
tasks into a dependency graph with explicit budgets, policies, acceptance
criteria, allowed/forbidden paths, and publication requirements.

Design invariants (consistent with evaluation/models.py and optimization/):

* Determinism: every artifact is serialized via ``canonical_json_bytes`` so
  SHA-256 hashes are reproducible across machines and runs.
* Integrity is fail-closed: roadmaps carry an ``integrity_sha256`` over their
  unsigned payload.
* Two-stage compilation: Stage 1 is deterministic (skeleton + budgets +
  policies); Stage 2 is ONE optional bounded model call that may only fill
  domain intent — it may NEVER expand paths, change policy, approve sensitive
  work, remove tests, increase budgets, or add providers.
* The validator owns the final accepted roadmap; a roadmap is only READY after
  deterministic validation passes.

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Mapping

from ..evaluation.models import canonical_json_bytes, sha256_json


SCHEMA_VERSION = 1


import re as _re

# A task_id is a filesystem/registry key. It must be a single safe component:
# no path separators, no traversal, no control characters. Matches the
# _safe_id format used by templates so existing IDs remain valid.
_SAFE_TASK_ID = _re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _validate_task_id(value: object, label: str = "task_id") -> str:
    s = str(value)
    if not _SAFE_TASK_ID.fullmatch(s) or s in {".", ".."}:
        raise ValueError(
            f"invalid {label}: {s!r} must be a single safe identifier "
            "(no path separators, traversal or control characters)"
        )
    return s


class RoadmapState(str, Enum):
    DRAFT = "DRAFT"
    VALIDATING = "VALIDATING"
    READY = "READY"
    BLOCKED = "BLOCKED"
    RUNNING = "RUNNING"
    PARTIAL = "PARTIAL"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"


class RiskClass(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    SENSITIVE = "SENSITIVE"


class ReviewRequirement(str, Enum):
    NONE = "NONE"
    CODEX_FINAL = "CODEX_FINAL"
    CODEX_WITH_CORRECTION = "CODEX_WITH_CORRECTION"
    HUMAN = "HUMAN"


class PublicationRequirement(str, Enum):
    NONE = "NONE"
    COMMIT_ONLY = "COMMIT_ONLY"
    COMMIT_AND_PR = "COMMIT_AND_PR"
    HUMAN_APPROVED = "HUMAN_APPROVED"


@dataclass(frozen=True)
class TaskBudget:
    """Resource bounds for one task."""

    max_model_calls: int = 4
    max_context_bytes: int = 48 * 1024
    max_wall_clock_seconds: int = 1800
    max_corrections: int = 1
    max_questions: int = 2

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_model_calls": self.max_model_calls,
            "max_context_bytes": self.max_context_bytes,
            "max_wall_clock_seconds": self.max_wall_clock_seconds,
            "max_corrections": self.max_corrections,
            "max_questions": self.max_questions,
        }


@dataclass(frozen=True)
class TaskSpec:
    """One task within a roadmap."""

    task_id: str
    title: str
    description: str
    category: str = "implementation"
    complexity: str = "SMALL"            # TRIVIAL/SMALL/MEDIUM/COMPLEX/SENSITIVE
    risk_class: str = "LOW"
    done_criteria: tuple[str, ...] = ()
    allowed_paths: tuple[str, ...] = ()
    forbidden_paths: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    tests: tuple[str, ...] = ()
    budget: TaskBudget = field(default_factory=TaskBudget)
    review: str = "CODEX_FINAL"          # ReviewRequirement value
    publication: str = "COMMIT_AND_PR"   # PublicationRequirement value
    parallel_group: str = "independent"  # none / independent / human
    stop_conditions: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    schema_version: int = SCHEMA_VERSION

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "title": self.title,
            "description": self.description,
            "category": self.category,
            "complexity": self.complexity,
            "risk_class": self.risk_class,
            "done_criteria": list(self.done_criteria),
            "allowed_paths": list(self.allowed_paths),
            "forbidden_paths": list(self.forbidden_paths),
            "dependencies": list(self.dependencies),
            "tests": list(self.tests),
            "budget": self.budget.to_dict(),
            "review": self.review,
            "publication": self.publication,
            "parallel_group": self.parallel_group,
            "stop_conditions": list(self.stop_conditions),
            "assumptions": list(self.assumptions),
            "unknowns": list(self.unknowns),
        }


@dataclass(frozen=True)
class Roadmap:
    """A compiled, content-addressed roadmap."""

    roadmap_id: str
    project_id: str
    objective: str
    assumptions: tuple[str, ...]
    tasks: tuple[TaskSpec, ...]
    acceptance_criteria: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    forbidden_paths: tuple[str, ...]
    risk_class: str
    review_requirements: str
    publication_requirements: str
    parallel_groups: tuple[tuple[str, ...], ...]   # groups of task_ids
    stop_conditions: tuple[str, ...]
    state: str = RoadmapState.DRAFT.value
    schema_version: int = SCHEMA_VERSION
    integrity_sha256: str = ""

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "roadmap_id": self.roadmap_id,
            "project_id": self.project_id,
            "objective": self.objective,
            "assumptions": list(self.assumptions),
            "tasks": [t.unsigned_dict() for t in self.tasks],
            "acceptance_criteria": list(self.acceptance_criteria),
            "allowed_paths": list(self.allowed_paths),
            "forbidden_paths": list(self.forbidden_paths),
            "risk_class": self.risk_class,
            "review_requirements": self.review_requirements,
            "publication_requirements": self.publication_requirements,
            "parallel_groups": [list(g) for g in self.parallel_groups],
            "stop_conditions": list(self.stop_conditions),
            "state": self.state,
        }

    def with_integrity(self) -> "Roadmap":
        return replace(self, integrity_sha256=sha256_json(self.unsigned_dict()))

    def verify_integrity(self) -> bool:
        return bool(self.integrity_sha256) and self.integrity_sha256 == sha256_json(
            self.unsigned_dict()
        )

    @property
    def roadmap_hash(self) -> str:
        """Content-addressed hash (same as integrity, explicit alias)."""
        return sha256_json(self.unsigned_dict())

    def to_dict(self) -> dict[str, Any]:
        payload = self.unsigned_dict()
        payload["integrity_sha256"] = self.integrity_sha256
        return payload

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Roadmap":
        tasks = tuple(
            TaskSpec(
                task_id=_validate_task_id(t["task_id"]),
                title=str(t["title"]),
                description=str(t["description"]),
                category=str(t.get("category", "implementation")),
                complexity=str(t.get("complexity", "SMALL")),
                risk_class=str(t.get("risk_class", "LOW")),
                done_criteria=tuple(t.get("done_criteria", [])),
                allowed_paths=tuple(t.get("allowed_paths", [])),
                forbidden_paths=tuple(t.get("forbidden_paths", [])),
                dependencies=tuple(t.get("dependencies", [])),
                tests=tuple(t.get("tests", [])),
                budget=TaskBudget(
                    max_model_calls=int(t.get("budget", {}).get("max_model_calls", 4)),
                    max_context_bytes=int(t.get("budget", {}).get("max_context_bytes", 49152)),
                    max_wall_clock_seconds=int(t.get("budget", {}).get("max_wall_clock_seconds", 1800)),
                    max_corrections=int(t.get("budget", {}).get("max_corrections", 1)),
                    max_questions=int(t.get("budget", {}).get("max_questions", 2)),
                ),
                review=str(t.get("review", "CODEX_FINAL")),
                publication=str(t.get("publication", "COMMIT_AND_PR")),
                parallel_group=str(t.get("parallel_group", "independent")),
                stop_conditions=tuple(t.get("stop_conditions", [])),
                assumptions=tuple(t.get("assumptions", [])),
                unknowns=tuple(t.get("unknowns", [])),
            )
            for t in d.get("tasks", [])
        )
        return cls(
            roadmap_id=str(d["roadmap_id"]),
            project_id=str(d["project_id"]),
            objective=str(d["objective"]),
            assumptions=tuple(d.get("assumptions", [])),
            tasks=tasks,
            acceptance_criteria=tuple(d.get("acceptance_criteria", [])),
            allowed_paths=tuple(d.get("allowed_paths", [])),
            forbidden_paths=tuple(d.get("forbidden_paths", [])),
            risk_class=str(d.get("risk_class", "LOW")),
            review_requirements=str(d.get("review_requirements", "CODEX_FINAL")),
            publication_requirements=str(d.get("publication_requirements", "COMMIT_AND_PR")),
            parallel_groups=tuple(tuple(g) for g in d.get("parallel_groups", [])),
            stop_conditions=tuple(d.get("stop_conditions", [])),
            state=str(d.get("state", RoadmapState.DRAFT.value)),
            schema_version=int(d.get("schema_version", SCHEMA_VERSION)),
            integrity_sha256=str(d.get("integrity_sha256", "")),
        )

    def task_ids(self) -> tuple[str, ...]:
        return tuple(t.task_id for t in self.tasks)

    def dependency_graph(self) -> dict[str, tuple[str, ...]]:
        return {t.task_id: t.dependencies for t in self.tasks}
