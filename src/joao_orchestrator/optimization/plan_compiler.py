"""Deterministic minimal execution-plan compiler (T2).

Compiles a task request + project profile into a frozen, content-addressed
ExecutionPlan. No LLM required; fully deterministic. The plan binds execution:
once compiled, a task cannot expand its own scope (the plan hash is recorded
and verified at each gate).

Reuses runtime/complexity.py for scoring (extends, does not duplicate).

Complexity tiers (spec): TRIVIAL / SMALL / MEDIUM / COMPLEX / SENSITIVE.
  SENSITIVE is always human-only; no provider may implement it.

Determinism: same inputs -> same plan and same plan hash. Stdlib only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Mapping, Optional

from ..evaluation.models import canonical_json_bytes, sha256_json
from ..runtime.complexity import estimate_complexity, complexity_tier


SCHEMA_VERSION = 1


class Complexity(str, Enum):
    TRIVIAL = "TRIVIAL"
    SMALL = "SMALL"
    MEDIUM = "MEDIUM"
    COMPLEX = "COMPLEX"
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


# --- Sensitive-path detection (deterministic, deny-by-default) --------------

# Paths that are always sensitive: policy, secrets, CI, auth, deployment.
SENSITIVE_PATH_PATTERNS = (
    r"^policy/",
    r"^\.github/workflows/",
    r"^\.env",
    r"secrets",
    r"credentials",
    r"auth/",
    r"^infra/",
    r"^deploy/",
    r"^migrations/",
)

_SENSITIVE_RE = [re.compile(p, re.IGNORECASE) for p in SENSITIVE_PATH_PATTERNS]


def is_sensitive_path(rel_path: str) -> bool:
    """Deterministic sensitive-path check. Deny-by-default for matches."""
    norm = rel_path.replace("\\", "/")
    # Strip only a leading "./" prefix (not arbitrary dots, which matter for
    # dotfiles like .env and .github/).
    while norm.startswith("./"):
        norm = norm[2:]
    for rx in _SENSITIVE_RE:
        if rx.search(norm):
            return True
    return False


# --- Complexity classification ----------------------------------------------

def classify_complexity(
    category: str,
    size: str,
    request_text: str,
    done_criteria: Optional[list[str]] = None,
    dependencies: Optional[list[str]] = None,
    touched_paths: Optional[list[str]] = None,
    sensitive_paths_present: bool = False,
) -> Complexity:
    """Map a task to a Complexity tier.

    Sensitive paths always force SENSITIVE regardless of other signals.
    Otherwise: category/size/keywords/done-criteria/deps drive the tier.
    """
    if sensitive_paths_present:
        return Complexity.SENSITIVE
    if touched_paths and any(is_sensitive_path(p) for p in touched_paths):
        return Complexity.SENSITIVE
    cat = (category or "").lower()
    if cat in ("security", "sensitive"):
        return Complexity.SENSITIVE

    est = estimate_complexity(
        category=cat or "implementation",
        size=size or "small",
        done_criteria=done_criteria,
        dependencies=dependencies,
        request_text=request_text or "",
    )
    # Map the existing 0-100 score to spec tiers.
    if est.score < 25:
        return Complexity.TRIVIAL
    if est.score < 45:
        return Complexity.SMALL
    if est.score < 65:
        return Complexity.MEDIUM
    return Complexity.COMPLEX


# --- Per-complexity budgets (spec T8 alignment) -----------------------------

@dataclass(frozen=True)
class ComplexityBudget:
    """Resource budgets bound to a complexity tier."""
    context_bytes: int
    question_budget: int
    correction_budget: int
    review: ReviewRequirement
    publication: PublicationRequirement
    max_files: int
    parallelism_group: str  # "none" / "independent" / "human"

    def to_dict(self) -> dict[str, Any]:
        return {
            "context_bytes": self.context_bytes,
            "question_budget": self.question_budget,
            "correction_budget": self.correction_budget,
            "review": self.review.value,
            "publication": self.publication.value,
            "max_files": self.max_files,
            "parallelism_group": self.parallelism_group,
        }


_BUDGETS: dict[Complexity, ComplexityBudget] = {
    Complexity.TRIVIAL: ComplexityBudget(
        context_bytes=8 * 1024, question_budget=0, correction_budget=0,
        review=ReviewRequirement.NONE, publication=PublicationRequirement.COMMIT_ONLY,
        max_files=3, parallelism_group="independent",
    ),
    Complexity.SMALL: ComplexityBudget(
        context_bytes=24 * 1024, question_budget=1, correction_budget=1,
        review=ReviewRequirement.CODEX_FINAL, publication=PublicationRequirement.COMMIT_AND_PR,
        max_files=6, parallelism_group="independent",
    ),
    Complexity.MEDIUM: ComplexityBudget(
        context_bytes=48 * 1024, question_budget=2, correction_budget=1,
        review=ReviewRequirement.CODEX_WITH_CORRECTION, publication=PublicationRequirement.COMMIT_AND_PR,
        max_files=12, parallelism_group="independent",
    ),
    Complexity.COMPLEX: ComplexityBudget(
        context_bytes=48 * 1024, question_budget=2, correction_budget=2,
        review=ReviewRequirement.CODEX_WITH_CORRECTION, publication=PublicationRequirement.HUMAN_APPROVED,
        max_files=12, parallelism_group="none",
    ),
    Complexity.SENSITIVE: ComplexityBudget(
        context_bytes=0, question_budget=0, correction_budget=0,
        review=ReviewRequirement.HUMAN, publication=PublicationRequirement.HUMAN_APPROVED,
        max_files=0, parallelism_group="human",
    ),
}


def budget_for(complexity: Complexity) -> ComplexityBudget:
    return _BUDGETS[complexity]


# --- Stop conditions --------------------------------------------------------

STOP_CONDITIONS = (
    "all_done_criteria_met",
    "review_verdict_PASS",
    "no_blocked_gates",
    "diff_touches_only_allowed_paths",
    "full_suite_green_once",
)

SENSITIVE_STOP_CONDITIONS = (
    "human_approval_required",
    "no_provider_implementation",
)


# --- The plan ---------------------------------------------------------------

@dataclass(frozen=True)
class ExecutionPlan:
    """A frozen, content-addressed execution plan for one task.

    Execution binds to this plan: the plan_sha256 is recorded at every gate
    and the task may not expand scope beyond allowed_files / context_bytes.
    """

    plan_id: str
    task_id: str
    project_id: str
    category: str
    complexity: Complexity
    required_capabilities: tuple[str, ...]
    likely_files: tuple[str, ...]
    allowed_files: tuple[str, ...]
    forbidden_files: tuple[str, ...]
    candidate_tests: tuple[str, ...]
    required_gates: tuple[str, ...]
    context_budget: ComplexityBudget
    stop_conditions: tuple[str, ...]
    sensitive: bool
    request_text: str
    schema_version: int = SCHEMA_VERSION
    plan_sha256: str = ""

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "task_id": self.task_id,
            "project_id": self.project_id,
            "category": self.category,
            "complexity": self.complexity.value,
            "required_capabilities": list(self.required_capabilities),
            "likely_files": list(self.likely_files),
            "allowed_files": list(self.allowed_files),
            "forbidden_files": list(self.forbidden_files),
            "candidate_tests": list(self.candidate_tests),
            "required_gates": list(self.required_gates),
            "context_budget": self.context_budget.to_dict(),
            "stop_conditions": list(self.stop_conditions),
            "sensitive": self.sensitive,
            "request_text": self.request_text,
        }

    def with_integrity(self) -> "ExecutionPlan":
        return replace(self, plan_sha256=sha256_json(self.unsigned_dict()))

    def verify_integrity(self) -> bool:
        return bool(self.plan_sha256) and self.plan_sha256 == sha256_json(
            self.unsigned_dict()
        )

    def to_dict(self) -> dict[str, Any]:
        payload = self.unsigned_dict()
        payload["plan_sha256"] = self.plan_sha256
        return payload


@dataclass(frozen=True)
class TaskRequest:
    """Minimal task request input to the compiler."""
    task_id: str
    project_id: str
    category: str = "implementation"
    size: str = "small"
    request_text: str = ""
    done_criteria: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    requested_paths: tuple[str, ...] = ()  # exact paths the task says it needs
    candidate_tests: tuple[str, ...] = ()
    publish: bool = False


@dataclass(frozen=True)
class ProjectProfileInput:
    """Subset of project profile the compiler uses."""
    project_id: str
    allowed_write_globs: tuple[str, ...] = ()
    forbidden_paths: tuple[str, ...] = ()
    requires_human_review: bool = False


def _safe_id(value: str) -> str:
    """Reduce a string to a safe plan_id component."""
    s = re.sub(r"[^A-Za-z0-9._-]", "_", value)
    return s[:64] or "plan"


def _likely_files(request_text: str, requested_paths: tuple[str, ...]) -> tuple[str, ...]:
    """Extract likely file paths from request text + explicit paths.

    Exact requested paths override guesses (spec rule). Lexical extraction
    only: matches tokens that look like file paths.
    """
    # Explicit paths first (they win).
    out: list[str] = []
    seen: set[str] = set()
    for p in requested_paths:
        if p and p not in seen:
            out.append(p)
            seen.add(p)
    # Lexical guess from request text: tokens with a slash and an extension.
    if request_text:
        for tok in re.findall(r"[A-Za-z0-9_./-]+\.[A-Za-z0-9]+", request_text):
            if "/" in tok and tok not in seen and not tok.startswith("http"):
                out.append(tok)
                seen.add(tok)
    return tuple(out)


def _forbidden_for(
    profile: ProjectProfileInput,
    sensitive_present: bool,
) -> tuple[str, ...]:
    out: list[str] = list(profile.forbidden_paths)
    if sensitive_present:
        out.extend(p for p in ("policy/", ".env", ".github/workflows/") if p not in out)
    return tuple(dict.fromkeys(out))  # dedupe preserving order


def _required_gates(complexity: Complexity) -> tuple[str, ...]:
    """Gate sequence required by the tier (spec T5 alignment)."""
    if complexity is Complexity.SENSITIVE:
        return ("human_approval",)
    if complexity is Complexity.TRIVIAL:
        return ("t0_syntax_import",)
    if complexity is Complexity.SMALL:
        return ("t0_syntax_import", "t1_unit", "t2_dependent", "codex_final_review")
    if complexity is Complexity.MEDIUM:
        return ("t0_syntax_import", "t1_unit", "t2_dependent", "t3_smoke",
                "codex_review_with_correction")
    return ("t0_syntax_import", "t1_unit", "t2_dependent", "t3_smoke", "t4_full",
            "codex_review_with_correction", "human_after_second_defect")


def compile_plan(
    request: TaskRequest,
    profile: ProjectProfileInput,
    repository_index: Optional[Mapping[str, Any]] = None,
    historical_metrics: Optional[Mapping[str, Any]] = None,
) -> ExecutionPlan:
    """Compile a TaskRequest + ProjectProfile into a frozen ExecutionPlan.

    Deterministic: no LLM, no clock-dependent logic. Sensitive paths force
    SENSITIVE (human-only). Exact requested paths override guesses.
    """
    # Sensitive detection across requested + likely paths.
    all_paths = list(request.requested_paths) + list(
        _likely_files(request.request_text, request.requested_paths)
    )
    sensitive_present = any(is_sensitive_path(p) for p in all_paths) or profile.requires_human_review

    complexity = classify_complexity(
        category=request.category,
        size=request.size,
        request_text=request.request_text,
        done_criteria=list(request.done_criteria),
        dependencies=list(request.dependencies),
        touched_paths=all_paths,
        sensitive_paths_present=sensitive_present,
    )

    budget = budget_for(complexity)
    likely = _likely_files(request.request_text, request.requested_paths)

    # Allowed files: exact requested paths if present, else likely files within
    # profile allow-globs. Sensitive -> none.
    if complexity is Complexity.SENSITIVE:
        allowed: tuple[str, ...] = ()
    elif request.requested_paths:
        allowed = tuple(dict.fromkeys(request.requested_paths))
    else:
        allowed = tuple(dict.fromkeys(likely))

    forbidden = _forbidden_for(profile, sensitive_present)

    # Required capabilities by tier.
    if complexity is Complexity.SENSITIVE:
        caps = ("task.approve",)
    else:
        caps = ("workspace.read", "workspace.write", "diff.inspect", "artifact.write")
        if request.publish and complexity is not Complexity.TRIVIAL:
            caps = caps + ("git.commit", "git.push")

    stops = SENSITIVE_STOP_CONDITIONS if complexity is Complexity.SENSITIVE else STOP_CONDITIONS

    plan = ExecutionPlan(
        plan_id=_safe_id(f"{request.project_id}-{request.task_id}"),
        task_id=request.task_id,
        project_id=request.project_id,
        category=request.category or "implementation",
        complexity=complexity,
        required_capabilities=tuple(caps),
        likely_files=likely,
        allowed_files=allowed,
        forbidden_files=forbidden,
        candidate_tests=request.candidate_tests,
        required_gates=_required_gates(complexity),
        context_budget=budget,
        stop_conditions=stops,
        sensitive=sensitive_present,
        request_text=request.request_text,
    )
    return plan.with_integrity()


def plan_allows_path(plan: ExecutionPlan, rel_path: str) -> bool:
    """Check whether a plan allows touching a path.

    Sensitive plans allow nothing. Otherwise: forbidden paths deny; if allowed
    files are listed, the path must match one; else profile allow-globs decide
    (handled by caller via policy.paths).
    """
    if plan.complexity is Complexity.SENSITIVE:
        return False
    norm = rel_path.replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    for f in plan.forbidden_files:
        if norm.startswith(f.rstrip("/")) or is_sensitive_path(norm):
            return False
    if plan.allowed_files:
        return any(norm == a or norm.startswith(a.rstrip("/") + "/") for a in plan.allowed_files)
    return True  # no explicit allow-list; caller's profile governs


def plan_allows_context_bytes(plan: ExecutionPlan, bytes_used: int) -> bool:
    """Check the context-byte budget (sensitive -> 0 allowed)."""
    if plan.complexity is Complexity.SENSITIVE:
        return bytes_used == 0
    return bytes_used <= plan.context_budget.context_bytes
