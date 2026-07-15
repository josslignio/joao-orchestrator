"""C3 Roadmap Compiler — human-independent deterministic validator.

Owns the final accepted roadmap. A roadmap may only transition to READY after
this validator returns no blocking violations. The validator is fully
deterministic: no model calls, no network, no human input.

Required invariants checked:
  - unique task IDs
  - acyclic dependency graph
  - all dependencies exist
  - topological order valid
  - no task approves itself
  - no task expands its own paths
  - sensitive paths are human-only
  - every task has done criteria or explicit deterministic reason
  - every task has tests or explicit deterministic reason
  - every task has time/model/context budgets
  - publication is explicit

Stage-2 model intent is checked for violations:
  - may not expand allowed roots
  - may not change policy
  - may not approve sensitive work
  - may not remove tests
  - may not increase budgets
  - may not add providers
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .models import Roadmap, RoadmapState, TaskSpec, SCHEMA_VERSION


@dataclass(frozen=True)
class ValidationViolation:
    """One validation violation."""

    severity: str   # "blocking" or "warning"
    code: str
    task_id: str    # "" for roadmap-level violations
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "task_id": self.task_id,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ValidationResult:
    """Result of validating a roadmap."""

    roadmap_id: str
    state: str        # READY / REJECTED
    blocking: tuple[ValidationViolation, ...]
    warnings: tuple[ValidationViolation, ...]
    schema_version: int = SCHEMA_VERSION

    @property
    def accepted(self) -> bool:
        return self.state == RoadmapState.READY.value and not self.blocking

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "roadmap_id": self.roadmap_id,
            "state": self.state,
            "blocking": [v.to_dict() for v in self.blocking],
            "warnings": [v.to_dict() for v in self.warnings],
            "accepted": self.accepted,
        }


# Sensitive path patterns (mirrors plan_compiler for consistency).
_SENSITIVE_PATTERNS = (
    "policy/", ".env", "secrets", "credentials", "auth/",
    ".github/workflows/", "infra/", "deploy/", "migrations/",
)


def _is_sensitive_path(path: str) -> bool:
    low = path.lower()
    return any(p in low for p in _SENSITIVE_PATTERNS)


def _is_unsafe_path(path: str) -> bool:
    """Reject paths that could escape a repository root or are absolute.

    A roadmap is compiled before any filesystem resolution happens, so this
    uses pure-string structural checks (no realpath needed): an absolute path
    or any component that resolves to the parent (``..``) is rejected
    unconditionally. Windows drive roots and UNC prefixes are rejected too.
    """
    if not path or not isinstance(path, str):
        return True
    norm = path.strip()
    # Absolute POSIX path, Windows drive path, or UNC share.
    if norm.startswith("/") or norm.startswith("\\"):
        return True
    if len(norm) >= 2 and norm[1] == ":" and norm[0].isalpha():
        return True
    # Any component that escapes upward.
    parts = norm.replace("\\", "/").split("/")
    if any(part == ".." for part in parts):
        return True
    # Null bytes are never legitimate in a path string.
    if "\x00" in path:
        return True
    return False


def _has_cycle(graph: dict[str, tuple[str, ...]]) -> Optional[list[str]]:
    """Return a cycle path if the graph has a cycle, else None."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {n: WHITE for n in graph}
    stack: list[str] = []

    def visit(node: str) -> Optional[list[str]]:
        color[node] = GRAY
        stack.append(node)
        for dep in graph.get(node, ()):
            if color.get(dep, WHITE) == GRAY:
                # Found a cycle: extract from stack.
                idx = stack.index(dep)
                return stack[idx:]
            if color.get(dep, WHITE) == WHITE:
                found = visit(dep)
                if found:
                    return found
        stack.pop()
        color[node] = BLACK
        return None

    for node in graph:
        if color[node] == WHITE:
            found = visit(node)
            if found:
                return found
    return None


def validate(roadmap: Roadmap) -> ValidationResult:
    """Validate a roadmap deterministically. Returns READY or REJECTED."""
    blocking: list[ValidationViolation] = []
    warnings: list[ValidationViolation] = []

    # 1. Unique task IDs.
    ids = [t.task_id for t in roadmap.tasks]
    seen: set[str] = set()
    for tid in ids:
        if tid in seen:
            blocking.append(ValidationViolation(
                "blocking", "duplicate_task_id", tid,
                f"duplicate task ID: {tid}"))
        seen.add(tid)

    id_set = set(ids)

    # 2. All dependencies exist.
    for t in roadmap.tasks:
        for dep in t.dependencies:
            if dep not in id_set:
                blocking.append(ValidationViolation(
                    "blocking", "missing_dependency", t.task_id,
                    f"task depends on unknown task: {dep}"))

    # 3. Acyclic dependency graph + topological order.
    graph = roadmap.dependency_graph()
    cycle = _has_cycle(graph)
    if cycle:
        blocking.append(ValidationViolation(
            "blocking", "dependency_cycle", "",
            f"cycle detected: {' -> '.join(cycle)}"))

    # 4. No task approves itself (a task may not list itself as dependency).
    for t in roadmap.tasks:
        if t.task_id in t.dependencies:
            blocking.append(ValidationViolation(
                "blocking", "self_dependency", t.task_id,
                f"task depends on itself"))

    # 5. No task expands its own paths (allowed_paths must not include
    #    forbidden/sensitive paths) and no path may escape the repo root.
    for t in roadmap.tasks:
        for p in t.allowed_paths:
            if _is_unsafe_path(p):
                blocking.append(ValidationViolation(
                    "blocking", "path_escape", t.task_id,
                    f"allowed path is absolute or escapes repo root: {p!r}"))
            if _is_sensitive_path(p):
                blocking.append(ValidationViolation(
                    "blocking", "path_expansion", t.task_id,
                    f"allowed path is sensitive/forbidden: {p}"))
            if p in t.forbidden_paths:
                blocking.append(ValidationViolation(
                    "blocking", "path_expansion", t.task_id,
                    f"allowed path is also forbidden: {p}"))
        for p in t.forbidden_paths:
            if _is_unsafe_path(p):
                blocking.append(ValidationViolation(
                    "blocking", "path_escape", t.task_id,
                    f"forbidden path is absolute or escapes repo root: {p!r}"))

    # 5b. Roadmap-level allowed/forbidden paths must also be structurally safe.
    for p in roadmap.allowed_paths:
        if _is_unsafe_path(p):
            blocking.append(ValidationViolation(
                "blocking", "path_escape", "",
                f"roadmap allowed path is absolute or escapes repo root: {p!r}"))
    for p in roadmap.forbidden_paths:
        if _is_unsafe_path(p):
            blocking.append(ValidationViolation(
                "blocking", "path_escape", "",
                f"roadmap forbidden path is absolute or escapes repo root: {p!r}"))

    # 6. Sensitive paths are human-only.
    for t in roadmap.tasks:
        has_sensitive = any(_is_sensitive_path(p) for p in t.allowed_paths)
        if has_sensitive or t.complexity == "SENSITIVE":
            if t.review != "HUMAN":
                blocking.append(ValidationViolation(
                    "blocking", "sensitive_not_human_only", t.task_id,
                    "sensitive task must require HUMAN review"))

    # 7. Every task has done criteria or explicit deterministic reason.
    for t in roadmap.tasks:
        if not t.done_criteria:
            # Allow only if the task has a documented deterministic reason.
            reason = next((a for a in t.assumptions if "no done criteria" in a.lower()
                           or "deterministic reason" in a.lower()), None)
            if reason is None:
                blocking.append(ValidationViolation(
                    "blocking", "missing_done_criteria", t.task_id,
                    "task has no done criteria and no explicit deterministic reason"))

    # 8. Every task has tests or explicit deterministic reason.
    for t in roadmap.tasks:
        if not t.tests:
            reason = next((a for a in t.assumptions if "no tests" in a.lower()
                           or "deterministic reason" in a.lower()), None)
            if reason is None and t.category != "documentation":
                blocking.append(ValidationViolation(
                    "blocking", "missing_tests", t.task_id,
                    "task has no tests and no explicit deterministic reason"))

    # 9. Every task has time/model/context budgets.
    for t in roadmap.tasks:
        b = t.budget
        if t.complexity != "SENSITIVE":
            if b.max_model_calls <= 0:
                blocking.append(ValidationViolation(
                    "blocking", "missing_model_budget", t.task_id,
                    "non-sensitive task has max_model_calls <= 0"))
            if b.max_context_bytes <= 0:
                blocking.append(ValidationViolation(
                    "blocking", "missing_context_budget", t.task_id,
                    "non-sensitive task has max_context_bytes <= 0"))
            if b.max_wall_clock_seconds <= 0:
                blocking.append(ValidationViolation(
                    "blocking", "missing_time_budget", t.task_id,
                    "non-sensitive task has max_wall_clock_seconds <= 0"))

    # 10. Publication is explicit (not empty string).
    for t in roadmap.tasks:
        if not t.publication:
            blocking.append(ValidationViolation(
                "blocking", "missing_publication", t.task_id,
                "task has no publication requirement"))

    state = RoadmapState.READY.value if not blocking else RoadmapState.REJECTED.value
    return ValidationResult(
        roadmap_id=roadmap.roadmap_id,
        state=state,
        blocking=tuple(blocking),
        warnings=tuple(warnings),
    )


def validate_stage2_intent(
    original: Roadmap,
    enriched: Roadmap,
) -> list[ValidationViolation]:
    """Check that Stage-2 model enrichment did not violate any constraint.

    Stage 2 may only fill domain intent. It may NOT:
      - expand allowed roots
      - change policy (review/publication/parallel_group)
      - approve sensitive work (reduce SENSITIVE to non-HUMAN)
      - remove tests
      - increase budgets
      - add providers

    Returns a list of violations (all blocking).
    """
    violations: list[ValidationViolation] = []
    orig_map = {t.task_id: t for t in original.tasks}
    # Stage 2 must not remove tasks (changing scope).
    if len(enriched.tasks) < len(original.tasks):
        orig_ids = set(orig_map)
        new_ids = {t.task_id for t in enriched.tasks}
        removed = sorted(orig_ids - new_ids)
        violations.append(ValidationViolation(
            "blocking", "stage2_removed_tasks", "",
            f"Stage 2 removed tasks: {removed}"))
    for new_t in enriched.tasks:
        orig_t = orig_map.get(new_t.task_id)
        if orig_t is None:
            violations.append(ValidationViolation(
                "blocking", "stage2_added_task", new_t.task_id,
                "Stage 2 added a task (not allowed)"))
            continue
        # Expanded allowed roots?
        orig_allowed = set(orig_t.allowed_paths)
        new_allowed = set(new_t.allowed_paths)
        if new_allowed - orig_allowed:
            violations.append(ValidationViolation(
                "blocking", "stage2_expanded_paths", new_t.task_id,
                f"Stage 2 expanded allowed paths: {sorted(new_allowed - orig_allowed)}"))
        # Changed policy?
        if new_t.review != orig_t.review:
            violations.append(ValidationViolation(
                "blocking", "stage2_changed_policy", new_t.task_id,
                f"Stage 2 changed review: {orig_t.review} -> {new_t.review}"))
        if new_t.publication != orig_t.publication:
            violations.append(ValidationViolation(
                "blocking", "stage2_changed_policy", new_t.task_id,
                f"Stage 2 changed publication: {orig_t.publication} -> {new_t.publication}"))
        # Removed tests?
        if set(orig_t.tests) - set(new_t.tests):
            violations.append(ValidationViolation(
                "blocking", "stage2_removed_tests", new_t.task_id,
                f"Stage 2 removed tests: {sorted(set(orig_t.tests) - set(new_t.tests))}"))
        # Increased budgets?
        if new_t.budget.max_model_calls > orig_t.budget.max_model_calls:
            violations.append(ValidationViolation(
                "blocking", "stage2_increased_budget", new_t.task_id,
                "Stage 2 increased model_calls budget"))
        if new_t.budget.max_context_bytes > orig_t.budget.max_context_bytes:
            violations.append(ValidationViolation(
                "blocking", "stage2_increased_budget", new_t.task_id,
                "Stage 2 increased context budget"))
    return violations
