"""Conflict-aware bounded worktree parallelism (T9).

Maximum two writer worktrees. Parallelize only when tasks are independent:
  - different projects OR disjoint allowed paths
  - no dependency relationship
  - no shared generated artifact
  - no shared migration/state mutation

Computes path/dependency/artifact/test/publication overlap before launch. If
uncertain, serialize (one writer at a time).

Rules (spec):
  - one writer per worktree;
  - independent runtime artifacts;
  - no shared staged path;
  - final integration serialized;
  - one failure cannot corrupt the other;
  - no auto-merge/force.

Target: two independent SMALL tasks <= 60% of sequential wall-clock.

Deterministic. No network. Stdlib only. Does NOT create worktrees itself —
it produces a parallelism plan that the caller executes via workspace/worktree.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Optional

from ..evaluation.models import sha256_json


SCHEMA_VERSION = 1
MAX_PARALLEL_WRITERS = 2


class ParallelismDecision(str, Enum):
    PARALLEL = "parallel"
    SERIAL = "serial"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class TaskPlan:
    """A task's parallelism-relevant footprint."""
    task_id: str
    project_id: str
    allowed_paths: tuple[str, ...]
    dependency_task_ids: tuple[str, ...] = ()
    artifact_paths: tuple[str, ...] = ()      # runtime artifacts produced
    test_paths: tuple[str, ...] = ()
    publication_paths: tuple[str, ...] = ()   # shared staged paths (e.g. a PR base)
    state_mutations: tuple[str, ...] = ()     # shared state files mutated
    complexity: str = "SMALL"

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "project_id": self.project_id,
            "allowed_paths": list(self.allowed_paths),
            "dependency_task_ids": list(self.dependency_task_ids),
            "artifact_paths": list(self.artifact_paths),
            "test_paths": list(self.test_paths),
            "publication_paths": list(self.publication_paths),
            "state_mutations": list(self.state_mutations),
            "complexity": self.complexity,
        }


def _paths_overlap(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    """Check if two path sets overlap (prefix-aware)."""
    set_a = {p.rstrip("/") for p in a}
    set_b = {p.rstrip("/") for p in b}
    # Direct match.
    if set_a & set_b:
        return True
    # Prefix overlap: one path is under the other.
    for pa in set_a:
        for pb in set_b:
            if pa.startswith(pb + "/") or pb.startswith(pa + "/"):
                return True
    return False


def _share_dependency(a: TaskPlan, b: TaskPlan) -> bool:
    """Check if a depends on b or b depends on a."""
    if a.task_id in b.dependency_task_ids or b.task_id in a.dependency_task_ids:
        return True
    # Transitive: shared dependency target.
    if set(a.dependency_task_ids) & set(b.dependency_task_ids):
        return True
    return False


@dataclass(frozen=True)
class OverlapReport:
    """The computed overlap between two tasks."""
    task_a: str
    task_b: str
    path_overlap: bool
    dependency_overlap: bool
    artifact_overlap: bool
    test_overlap: bool
    publication_overlap: bool
    state_overlap: bool
    same_project: bool
    can_parallelize: bool
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_a": self.task_a, "task_b": self.task_b,
            "path_overlap": self.path_overlap,
            "dependency_overlap": self.dependency_overlap,
            "artifact_overlap": self.artifact_overlap,
            "test_overlap": self.test_overlap,
            "publication_overlap": self.publication_overlap,
            "state_overlap": self.state_overlap,
            "same_project": self.same_project,
            "can_parallelize": self.can_parallelize,
            "reasons": list(self.reasons),
        }


def compute_overlap(a: TaskPlan, b: TaskPlan) -> OverlapReport:
    """Compute the overlap between two task plans."""
    reasons: list[str] = []
    path_overlap = _paths_overlap(a.allowed_paths, b.allowed_paths)
    dep_overlap = _share_dependency(a, b)
    artifact_overlap = _paths_overlap(a.artifact_paths, b.artifact_paths)
    test_overlap = _paths_overlap(a.test_paths, b.test_paths)
    pub_overlap = _paths_overlap(a.publication_paths, b.publication_paths)
    state_overlap = _paths_overlap(a.state_mutations, b.state_mutations)
    same_project = a.project_id == b.project_id

    can_parallelize = True
    # Disjoint allowed paths OR different projects is required.
    if same_project and path_overlap:
        can_parallelize = False
        reasons.append("same project + overlapping allowed paths")
    if dep_overlap:
        can_parallelize = False
        reasons.append("dependency relationship")
    if artifact_overlap:
        can_parallelize = False
        reasons.append("shared generated artifact")
    if state_overlap:
        can_parallelize = False
        reasons.append("shared state mutation")
    if pub_overlap:
        can_parallelize = False
        reasons.append("shared publication path")
    # Test overlap alone does not block parallelism (tests are read-only runs
    # in independent worktrees), but we record it.
    if test_overlap and same_project:
        reasons.append("shared test paths (informational)")

    if can_parallelize:
        reasons.append("independent: disjoint paths, no deps, no shared artifacts/state")

    return OverlapReport(
        task_a=a.task_id, task_b=b.task_id,
        path_overlap=path_overlap, dependency_overlap=dep_overlap,
        artifact_overlap=artifact_overlap, test_overlap=test_overlap,
        publication_overlap=pub_overlap, state_overlap=state_overlap,
        same_project=same_project,
        can_parallelize=can_parallelize, reasons=tuple(reasons),
    )


@dataclass(frozen=True)
class ParallelismPlan:
    """A plan for executing a set of tasks with bounded parallelism."""
    tasks: tuple[TaskPlan, ...]
    decision: ParallelismDecision
    waves: tuple[tuple[str, ...], ...]   # each wave = task_ids to run together
    max_writers: int = MAX_PARALLEL_WRITERS
    reasons: tuple[str, ...] = ()
    schema_version: int = SCHEMA_VERSION

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "tasks": [t.to_dict() for t in self.tasks],
            "decision": self.decision.value,
            "waves": [list(w) for w in self.waves],
            "max_writers": self.max_writers,
            "reasons": list(self.reasons),
        }

    def with_integrity(self) -> "ParallelismPlan":
        return replace(self)  # hash computed below

    @property
    def integrity_sha256(self) -> str:
        return sha256_json(self.unsigned_dict())

    def verify_integrity(self, expected: str) -> bool:
        return bool(expected) and expected == self.integrity_sha256

    def to_dict(self) -> dict[str, Any]:
        d = self.unsigned_dict()
        d["integrity_sha256"] = self.integrity_sha256
        return d


def plan_parallelism(tasks: tuple[TaskPlan, ...]) -> ParallelismPlan:
    """Produce a bounded parallelism plan for a set of tasks.

    Algorithm:
      1. Build a conflict graph (edge = cannot parallelize).
      2. Greedily partition into waves of at most MAX_PARALLEL_WRITERS where
         every pair in a wave can parallelize.
      3. If any single task blocks all others, serialize.
    """
    if not tasks:
        return ParallelismPlan(
            tasks=(), decision=ParallelismDecision.SERIAL, waves=(),
            reasons=("no tasks",),
        )

    # Single task: serial (one writer).
    if len(tasks) == 1:
        return ParallelismPlan(
            tasks=tasks, decision=ParallelismDecision.SERIAL,
            waves=((tasks[0].task_id,),),
            reasons=("single task: serial",),
        )

    # Compute pairwise overlap.
    can_pair: dict[tuple[str, str], bool] = {}
    for i, a in enumerate(tasks):
        for b in tasks[i + 1:]:
            rep = compute_overlap(a, b)
            can_pair[tuple(sorted((a.task_id, b.task_id)))] = rep.can_parallelize

    # Greedy wave assignment.
    remaining = list(tasks)
    waves: list[tuple[str, ...]] = []
    reasons: list[str] = []
    while remaining:
        wave: list[TaskPlan] = [remaining.pop(0)]
        i = 0
        while i < len(remaining) and len(wave) < MAX_PARALLEL_WRITERS:
            candidate = remaining[i]
            if all(can_pair[tuple(sorted((w.task_id, candidate.task_id)))] for w in wave):
                wave.append(remaining.pop(i))
            else:
                i += 1
        waves.append(tuple(t.task_id for t in wave))

    decision = ParallelismDecision.PARALLEL if len(waves) < len(tasks) else ParallelismDecision.SERIAL
    if any(not v for v in can_pair.values()):
        reasons.append("some task pairs cannot parallelize; serialized where needed")

    return ParallelismPlan(
        tasks=tasks, decision=decision, waves=tuple(waves),
        reasons=tuple(reasons) or ("all tasks parallelizable",),
    )


def can_launch_parallel(plan: ParallelismPlan) -> bool:
    """Whether the plan allows any parallel wave."""
    return plan.decision is ParallelismDecision.PARALLEL and any(len(w) > 1 for w in plan.waves)
