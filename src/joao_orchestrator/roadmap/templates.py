"""C3 Roadmap Compiler — deterministic templates.

A template is a deterministic function that produces a task skeleton + budgets
+ policies from a project objective + repository index. No model calls: Stage 1
only. The compiler selects a template by keyword matching on the objective.

Templates are intentionally conservative: they set tight budgets, forbid
sensitive paths, and require explicit acceptance criteria.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional

from .models import (
    Roadmap, RoadmapState, TaskSpec, TaskBudget,
    RiskClass, ReviewRequirement, PublicationRequirement, SCHEMA_VERSION,
)
from ..evaluation.models import sha256_json


def _slug(text: str, limit: int = 48) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", (text or "").strip().lower()).strip("-")
    return (s or "objective")[:limit].rstrip("-")


def _roadmap_id(project_id: str, objective: str) -> str:
    return f"{_slug(project_id, 24)}-{_slug(objective, 48)}"


def _safe_id(value: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]", "_", value)
    return s[:64] or "task"


# Shared default budgets by complexity.
_BUDGETS = {
    "TRIVIAL": TaskBudget(max_model_calls=2, max_context_bytes=8 * 1024,
                          max_wall_clock_seconds=600, max_corrections=0, max_questions=0),
    "SMALL": TaskBudget(max_model_calls=4, max_context_bytes=24 * 1024,
                        max_wall_clock_seconds=1200, max_corrections=1, max_questions=2),
    "MEDIUM": TaskBudget(max_model_calls=6, max_context_bytes=48 * 1024,
                         max_wall_clock_seconds=2400, max_corrections=1, max_questions=2),
    "COMPLEX": TaskBudget(max_model_calls=8, max_context_bytes=48 * 1024,
                          max_wall_clock_seconds=3600, max_corrections=2, max_questions=2),
    "SENSITIVE": TaskBudget(max_model_calls=0, max_context_bytes=0,
                            max_wall_clock_seconds=0, max_corrections=0, max_questions=0),
}


def _budget_for(complexity: str) -> TaskBudget:
    return _BUDGETS.get(complexity, _BUDGETS["SMALL"])


def _review_for(complexity: str) -> str:
    if complexity == "SENSITIVE":
        return ReviewRequirement.HUMAN.value
    if complexity == "TRIVIAL":
        return ReviewRequirement.NONE.value
    if complexity == "COMPLEX":
        return ReviewRequirement.CODEX_WITH_CORRECTION.value
    return ReviewRequirement.CODEX_FINAL.value


def _publication_for(complexity: str) -> str:
    if complexity == "SENSITIVE":
        return PublicationRequirement.HUMAN_APPROVED.value
    if complexity == "TRIVIAL":
        return PublicationRequirement.COMMIT_ONLY.value
    if complexity == "COMPLEX":
        return PublicationRequirement.HUMAN_APPROVED.value
    return PublicationRequirement.COMMIT_AND_PR.value


def _risk_for(complexity: str, sensitive: bool) -> str:
    if sensitive or complexity == "SENSITIVE":
        return RiskClass.SENSITIVE.value
    if complexity in ("COMPLEX", "MEDIUM"):
        return RiskClass.MEDIUM.value
    return RiskClass.LOW.value


def _common_stop_conditions(sensitive: bool) -> tuple[str, ...]:
    if sensitive:
        return ("human_approval_required", "no_provider_implementation")
    return (
        "all_done_criteria_met",
        "review_verdict_PASS",
        "no_blocked_gates",
        "diff_touches_only_allowed_paths",
        "full_suite_green_once",
    )


# ---------------------------------------------------------------------------
# Template registry
# ---------------------------------------------------------------------------

class TemplateResult:
    """Intermediate result from a template before integrity hashing."""

    def __init__(self, tasks: list[TaskSpec], assumptions: list[str],
                 acceptance: list[str], risk: str, review: str, publication: str):
        self.tasks = tasks
        self.assumptions = assumptions
        self.acceptance = acceptance
        self.risk = risk
        self.review = review
        self.publication = publication


def _signal_analysis_template(project_id: str, objective: str) -> TemplateResult:
    """Template for signal/backtest/performance-analysis objectives (e.g. R5).

    Produces a linear dependency chain: ledger -> backtest -> analytics ->
    scoring -> report. Each task is bounded and fixture-first.
    """
    prefix = _safe_id(project_id)
    tasks: list[TaskSpec] = []
    chain = [
        ("r5-1-ledger", "Historical signal ledger",
         "Create an append-only normalized history of signals with idempotent "
         "ingestion, deduplication, immutable source references, and content hashes.",
         "MEDIUM",
         ("src/r5/ledger.py", "tests/test_r5_ledger.py")),
        ("r5-2-backtest", "Deterministic backtest engine",
         "Compute deterministic forward-return metrics (1d/7d/30d, hit rate, "
         "median/mean) with no look-ahead and honest unavailable handling.",
         "MEDIUM",
         ("src/r5/backtest.py", "tests/test_r5_backtest.py")),
        ("r5-3-kol", "KOL performance analytics",
         "Aggregate per-KOL metrics: sample count, coverage, hit rate by "
         "horizon, consistency, recency-weighted score. No rewarding tiny samples.",
         "MEDIUM",
         ("src/r5/kol_performance.py", "tests/test_r5_kol.py")),
        ("r5-4-score", "Reliability and opportunity scoring",
         "Explainable deterministic score with persisted components. No opaque "
         "LLM scoring. Persist every component.",
         "MEDIUM",
         ("src/r5/scoring.py", "tests/test_r5_scoring.py")),
        ("r5-5-report", "Weekly report integration",
         "Add R5 sections to the report with backward compatibility (no R5 "
         "data -> existing report unchanged).",
         "SMALL",
         ("src/r5/report_integration.py", "tests/test_r5_report.py")),
    ]
    prev_id = ""
    for tid, title, desc, complexity, (src, test) in chain:
        deps = (prev_id,) if prev_id else ()
        sensitive = False
        t = TaskSpec(
            task_id=f"{prefix}-{tid}",
            title=title,
            description=desc,
            category="implementation",
            complexity=complexity,
            risk_class=_risk_for(complexity, sensitive),
            done_criteria=(f"{src} exists and passes tests", f"{test} exists and passes"),
            allowed_paths=(src, test),
            forbidden_paths=("policy/", ".env", ".github/workflows/"),
            dependencies=deps,
            tests=(test,),
            budget=_budget_for(complexity),
            review=_review_for(complexity),
            publication=_publication_for(complexity),
            parallel_group="none",  # linear chain
            stop_conditions=_common_stop_conditions(sensitive),
            assumptions=("fixture/offline-first data", "no look-ahead"),
        )
        tasks.append(t)
        prev_id = t.task_id

    parallel_groups = (tuple(t.task_id for t in tasks),)  # serial chain documented
    assumptions = [
        "fixture/offline-first historical data",
        "no live trading, no order execution",
        "no look-ahead at signal time",
        "R4 price-at-signal and forward-return artifacts available",
    ]
    acceptance = [
        "all R5 checkpoints pass targeted tests",
        "golden run produces identical hashes on rerun",
        "0 no-look-ahead violations",
        "0 missing required provenance",
        "report unchanged without R5 data (backward compatibility)",
    ]
    return TemplateResult(tasks, assumptions, acceptance,
                          RiskClass.MEDIUM.value,
                          ReviewRequirement.CODEX_WITH_CORRECTION.value,
                          PublicationRequirement.COMMIT_AND_PR.value)


def _generic_feature_template(project_id: str, objective: str) -> TemplateResult:
    """Conservative default template: one bounded implementation + tests."""
    prefix = _safe_id(project_id)
    tid = f"{prefix}-impl"
    t = TaskSpec(
        task_id=tid,
        title=f"Implement: {objective[:80]}",
        description=objective,
        category="implementation",
        complexity="SMALL",
        risk_class=RiskClass.LOW.value,
        done_criteria=("implementation passes targeted tests",),
        allowed_paths=("src/", "tests/"),
        forbidden_paths=("policy/", ".env", ".github/workflows/"),
        dependencies=(),
        tests=("tests/",),
        budget=_budget_for("SMALL"),
        review=_review_for("SMALL"),
        publication=_publication_for("SMALL"),
        parallel_group="independent",
        stop_conditions=_common_stop_conditions(False),
        assumptions=("objective is implementable within budget",),
    )
    return TemplateResult(
        [t],
        ["objective is implementable within budget"],
        ["implementation passes targeted tests", "no regression in existing suite"],
        RiskClass.LOW.value,
        ReviewRequirement.CODEX_FINAL.value,
        PublicationRequirement.COMMIT_AND_PR.value,
    )


def _documentation_template(project_id: str, objective: str) -> TemplateResult:
    """Documentation-only template: TRIVIAL, no review, commit only."""
    prefix = _safe_id(project_id)
    tid = f"{prefix}-docs"
    t = TaskSpec(
        task_id=tid,
        title=f"Documentation: {objective[:80]}",
        description=objective,
        category="documentation",
        complexity="TRIVIAL",
        risk_class=RiskClass.LOW.value,
        done_criteria=("documentation file exists with required content",),
        allowed_paths=("docs/", "README.md"),
        forbidden_paths=("policy/", ".env", "src/"),
        dependencies=(),
        tests=(),
        budget=_budget_for("TRIVIAL"),
        review=_review_for("TRIVIAL"),
        publication=_publication_for("TRIVIAL"),
        parallel_group="independent",
        stop_conditions=_common_stop_conditions(False),
    )
    return TemplateResult(
        [t],
        [],
        ["documentation file exists with required content"],
        RiskClass.LOW.value,
        ReviewRequirement.NONE.value,
        PublicationRequirement.COMMIT_ONLY.value,
    )


# Keyword -> template mapping. First match wins. Ordered by specificity.
_TEMPLATE_MATCHERS: list[tuple[tuple[str, ...], Any]] = [
    (("signal", "backtest", "kol", "performance", "radar", "r5"), _signal_analysis_template),
    (("documentation", "docs", "readme"), _documentation_template),
]


def select_template(objective: str) -> Any:
    """Select a template function by keyword matching on the objective."""
    low = (objective or "").lower()
    for keywords, fn in _TEMPLATE_MATCHERS:
        if any(k in low for k in keywords):
            return fn
    return _generic_feature_template


def build_draft(
    project_id: str,
    objective: str,
    repository_index: Optional[Mapping[str, Any]] = None,
    active_decisions: Optional[Mapping[str, Any]] = None,
) -> Roadmap:
    """Stage 1 deterministic compilation: build a DRAFT roadmap from a template.

    No model calls. The draft is content-addressed and in DRAFT state; the
    validator must transition it to READY before execution.
    """
    template_fn = select_template(objective)
    result = template_fn(project_id, objective)
    parallel_groups = (tuple(t.task_id for t in result.tasks),)
    rm = Roadmap(
        roadmap_id=_roadmap_id(project_id, objective),
        project_id=project_id,
        objective=objective,
        assumptions=tuple(result.assumptions),
        tasks=tuple(result.tasks),
        acceptance_criteria=tuple(result.acceptance),
        allowed_paths=tuple(sorted({p for t in result.tasks for p in t.allowed_paths})),
        forbidden_paths=tuple(sorted({p for t in result.tasks for p in t.forbidden_paths})),
        risk_class=result.risk,
        review_requirements=result.review,
        publication_requirements=result.publication,
        parallel_groups=parallel_groups,
        stop_conditions=("all_tasks_completed", "no_blocked_gates"),
        state=RoadmapState.DRAFT.value,
    )
    return rm.with_integrity()
