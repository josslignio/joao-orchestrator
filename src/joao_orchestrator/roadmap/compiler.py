"""C3 Roadmap Compiler — two-stage compilation.

Stage 1 (deterministic): resolve project, load profile, load repository index,
load active decisions, select template, construct dependency skeleton, assign
budgets and policies.

Stage 2 (one bounded model call, only when necessary): fill domain-specific
task intent, propose acceptance details, identify explicit unknowns. Stage 2 may
NOT expand allowed roots, change policy, approve sensitive work, remove tests,
increase budgets, or add providers.

The validator owns the final accepted roadmap. Compiled tasks feed the existing
queue and C2 goal loop (no second queue).

Deterministic. No network. Stdlib only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from .models import Roadmap, RoadmapState, TaskSpec, SCHEMA_VERSION
from .templates import build_draft
from .validator import validate, validate_stage2_intent, ValidationResult


@dataclass(frozen=True)
class CompilationInput:
    """Inputs to the compiler."""

    project_id: str
    objective: str
    repository_index: Optional[Mapping[str, Any]] = None
    active_decisions: Optional[Mapping[str, Any]] = None
    profile: Optional[Mapping[str, Any]] = None


@dataclass(frozen=True)
class CompilationResult:
    """Result of compiling a roadmap."""

    roadmap: Roadmap
    validation: ValidationResult
    stage2_used: bool
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "roadmap": self.roadmap.to_dict(),
            "validation": self.validation.to_dict(),
            "stage2_used": self.stage2_used,
        }


def compile_roadmap(
    project_id: str,
    objective: str,
    repository_index: Optional[Mapping[str, Any]] = None,
    active_decisions: Optional[Mapping[str, Any]] = None,
    stage2_fn: Optional[Callable[[Roadmap], Roadmap]] = None,
) -> CompilationResult:
    """Two-stage roadmap compilation.

    Stage 1 always runs (deterministic). Stage 2 runs only if ``stage2_fn`` is
    provided AND Stage 1 produced a valid draft. Stage-2 output is checked for
    constraint violations; any violation rejects the enrichment and the Stage-1
    draft is used.

    Returns a CompilationResult with the final roadmap + validation. The
    roadmap is in DRAFT/REJECTED state until the caller transitions it.
    """
    # Fail closed on unknown/invalid project identity.
    from ..domain.identifiers import validate_identifier
    validate_identifier(project_id, "project_id")
    if not objective or not objective.strip():
        raise ValueError("objective is required and must be non-empty")

    # Stage 1: deterministic draft.
    draft = build_draft(
        project_id=project_id,
        objective=objective,
        repository_index=repository_index,
        active_decisions=active_decisions,
    )

    stage2_used = False
    candidate = draft

    # Stage 2: optional bounded model enrichment.
    if stage2_fn is not None:
        enriched = stage2_fn(draft)
        if isinstance(enriched, Roadmap):
            violations = validate_stage2_intent(draft, enriched)
            if not violations:
                candidate = enriched
                stage2_used = True
            # If violations, silently fall back to the draft (Stage 2 rejected).

    # Validate the candidate.
    result = validate(candidate)
    # Transition state: READY if accepted, REJECTED otherwise.
    from dataclasses import replace
    final_state = RoadmapState.READY.value if result.accepted else RoadmapState.REJECTED.value
    candidate = replace(candidate, state=final_state)
    candidate = candidate.with_integrity()

    return CompilationResult(
        roadmap=candidate,
        validation=result,
        stage2_used=stage2_used,
    )


def materialize_to_queue(roadmap: Roadmap) -> list[dict[str, Any]]:
    """Materialize a READY roadmap's tasks into queue items (exactly once).

    Each task becomes a queue-item dict with: task_id, project_id, title,
    description, dependencies, complexity, done_criteria, allowed_paths,
    budget, review, publication. The caller enqueues these into the existing
    C2 queue. This function does NOT enqueue — it only materializes.

    Idempotent: the same roadmap always materializes to the same items in the
    same order (topological).
    """
    if roadmap.state != RoadmapState.READY.value:
        raise ValueError(
            f"cannot materialize roadmap in state {roadmap.state}; must be READY")

    # Topological order (Kahn's algorithm) for deterministic ordering.
    graph = roadmap.dependency_graph()
    in_degree: dict[str, int] = {tid: 0 for tid in graph}
    for tid, deps in graph.items():
        in_degree[tid] = len(deps)
    # Process in deterministic order (sorted by task_id within same in-degree).
    order: list[str] = []
    available = sorted(tid for tid, d in in_degree.items() if d == 0)
    while available:
        node = available.pop(0)
        order.append(node)
        # Decrement in-degree of dependents.
        for tid, deps in graph.items():
            if node in deps:
                in_degree[tid] -= 1
                if in_degree[tid] == 0:
                    # Insert in sorted position.
                    import bisect
                    bisect.insort(available, tid)

    task_map = {t.task_id: t for t in roadmap.tasks}
    items: list[dict[str, Any]] = []
    for tid in order:
        t = task_map[tid]
        items.append({
            "task_id": t.task_id,
            "project_id": roadmap.project_id,
            "title": t.title,
            "description": t.description,
            "category": t.category,
            "complexity": t.complexity,
            "dependencies": list(t.dependencies),
            "done_criteria": list(t.done_criteria),
            "allowed_paths": list(t.allowed_paths),
            "forbidden_paths": list(t.forbidden_paths),
            "tests": list(t.tests),
            "budget": t.budget.to_dict(),
            "review": t.review,
            "publication": t.publication,
            "roadmap_id": roadmap.roadmap_id,
            "roadmap_hash": roadmap.roadmap_hash,
        })
    return items
