"""GPT-first provider router with budget-aware selection.

Routes coding tasks to the best available provider using:
  - explicitly requested engine or auto;
  - task category (architecture, security, implementation, tests,
    documentation, review);
  - task size (small, medium, large);
  - verified provider availability;
  - budget snapshot and freshness;
  - project policy.

Routing policy:
  1. Explicit provider wins when locally verified, budget-permitted,
     profile-allowed, size-appropriate.
  2. Default GPT-first: Codex for architecture, security, complex debugging,
     reviews. GLM for predictable implementation, repetitive tests,
     documentation — only when opencode-zai is locally verified.
  3. Budget thresholds: >50% normal, 25-50% reduced context, 10-25%
     priority/small only, <10% reject medium/large.
  4. Unknown/expired budget: never invent quota; mark unknown/stale.
  5. Neither provider usable: fail closed, no API fallback.
  6. Never route Claude automatically.

Every routing result includes a selected provider (or none), machine-readable
reason code, human-readable reason, budget snapshot age, context policy,
fallback candidates, and human override requirement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

from .base import ProviderAdapter
from .budget import (
    BudgetStatus,
    BudgetStore,
    ProviderBudget,
    _classify_status,
    status_for_task,
)
from .opencode_provider import OpenCodeProvider


# Reason codes for routing decisions.
class ReasonCode(str, Enum):
    EXPLICIT_REQUEST = "explicit_request"
    GPT_FIRST_ARCHITECTURE = "gpt_first_architecture"
    GPT_FIRST_SECURITY = "gpt_first_security"
    GPT_FIRST_REVIEW = "gpt_first_review"
    GPT_FIRST_DEFAULT = "gpt_first_default"
    GLM_VERIFIED_IMPLEMENTATION = "glm_verified_implementation"
    GLM_VERIFIED_TESTS = "glm_verified_tests"
    GLM_VERIFIED_DOCUMENTATION = "glm_verified_documentation"
    GLM_FALLBACK = "glm_fallback"
    PROVIDER_EXHAUSTED = "provider_exhausted"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    BUDGET_UNKNOWN = "budget_unknown"
    BUDGET_STALE = "budget_stale"
    BUDGET_EXHAUSTED_SIZE = "budget_exhausted_for_size"
    PROFILE_REJECTED = "profile_rejected"
    NO_PROVIDER = "no_provider"
    TASK_TOO_LARGE = "task_too_large_for_budget"
    OVERRIDE_REQUIRED = "override_required"


# Task categories for routing.
TASK_CATEGORIES = (
    "architecture", "security", "implementation", "tests",
    "documentation", "review",
)

# Task sizes for routing.
TASK_SIZES = ("small", "medium", "large")

# Categories where Codex is preferred (GPT-first).
CODEX_PREFERRED_CATEGORIES = frozenset({
    "architecture", "security", "review",
})

# Categories where GLM may be used when verified.
GLM_ACCEPTABLE_CATEGORIES = frozenset({
    "implementation", "tests", "documentation",
})


@dataclass(frozen=True)
class ContextPolicy:
    """Context window policy based on budget level."""
    label: str = "full"
    max_tokens_hint: Optional[int] = None


@dataclass
class RoutingResult:
    """Complete routing decision with audit trail."""
    selected_provider: Optional[str]
    reason_code: str = ReasonCode.NO_PROVIDER.value
    reason_human: str = "no provider available"
    budget_snapshot_age_seconds: Optional[float] = None
    context_policy: Optional[ContextPolicy] = None
    fallback_candidates: List[str] = field(default_factory=list)
    human_override_required: bool = False
    budget_status: str = BudgetStatus.UNKNOWN.value
    provider_verified: bool = False
    task_category: str = ""
    task_size: str = ""

    def to_dict(self) -> dict:
        d = {
            "selected_provider": self.selected_provider,
            "reason_code": self.reason_code,
            "reason_human": self.reason_human,
            "fallback_candidates": self.fallback_candidates,
            "human_override_required": self.human_override_required,
            "budget_status": self.budget_status,
            "provider_verified": self.provider_verified,
            "task_category": self.task_category,
            "task_size": self.task_size,
        }
        if self.context_policy is not None:
            d["context_policy"] = {
                "label": self.context_policy.label,
                "max_tokens_hint": self.context_policy.max_tokens_hint,
            }
        if self.budget_snapshot_age_seconds is not None:
            d["budget_snapshot_age_seconds"] = self.budget_snapshot_age_seconds
        return d


@dataclass
class RoutingRequest:
    """Inputs to the routing decision."""
    engine: str = "auto"  # auto | codex-subscription | opencode-zai | ...
    task_category: str = "implementation"
    task_size: str = "small"
    project_id: str = ""

    # Provider availability from Phase 1 probes.
    codex_available: bool = False
    codex_verified: bool = False
    opencode_available: bool = False
    opencode_verified: bool = False

    # Project policy constraints (empty = no restrictions).
    allowed_providers: List[str] = field(default_factory=list)


def _context_policy_for_budget(remaining: Optional[float]) -> ContextPolicy:
    """Determine context policy from remaining budget percent."""
    if remaining is None:
        return ContextPolicy(label="conservative")
    if remaining > 50:
        return ContextPolicy(label="full")
    if remaining > 25:
        return ContextPolicy(label="reduced")
    if remaining > 10:
        return ContextPolicy(label="priority")
    return ContextPolicy(label="minimal")


def _budget_snapshot_age(budget: Optional[ProviderBudget]) -> Optional[float]:
    """Return age of budget snapshot in seconds, or None if no timestamp."""
    if budget is None or not budget.captured_at:
        return None
    try:
        from datetime import datetime, timezone
        captured = datetime.fromisoformat(
            budget.captured_at.replace("Z", "+00:00")
        )
        age = datetime.now(timezone.utc) - captured
        return age.total_seconds()
    except (ValueError, OSError):
        return None


def route(
    request: RoutingRequest,
    budget: Optional[ProviderBudget] = None,
    opencode_budget: Optional[ProviderBudget] = None,
) -> RoutingResult:
    """Route a task to the best available provider.

    Implements the GPT-first routing policy with budget-aware selection.
    Never enables an API fallback. Never routes Claude automatically.

    Returns a RoutingResult with full audit trail.
    """
    task_cat = request.task_category.lower()
    task_size = request.task_size.lower()

    # Normalize category.
    if task_cat not in TASK_CATEGORIES:
        task_cat = "implementation"

    # --- Explicit engine request ---
    if request.engine != "auto":
        return _route_explicit(request, budget, opencode_budget,
                               task_cat, task_size)

    # --- Automatic GPT-first routing ---
    return _route_auto(request, budget, opencode_budget, task_cat, task_size)


def _route_explicit(
    request: RoutingRequest,
    budget: Optional[ProviderBudget],
    opencode_budget: Optional[ProviderBudget],
    task_cat: str,
    task_size: str,
) -> RoutingResult:
    """Route an explicitly requested provider."""
    engine = request.engine
    fallbacks = []

    if engine == "codex-subscription":
        # Check project policy.
        if request.allowed_providers and "codex-subscription" not in request.allowed_providers:
            return RoutingResult(
                selected_provider=None,
                reason_code=ReasonCode.PROFILE_REJECTED.value,
                reason_human="codex-subscription rejected by project profile",
                task_category=task_cat,
                task_size=task_size,
            )
        # Check availability.
        if not request.codex_available or not request.codex_verified:
            return RoutingResult(
                selected_provider=None,
                reason_code=ReasonCode.PROVIDER_UNAVAILABLE.value,
                reason_human="codex-subscription not locally verified",
                fallback_candidates=["opencode-zai"] if request.opencode_verified else [],
                task_category=task_cat,
                task_size=task_size,
            )
        # Check budget.
        eff_status, budget_age, ctx = _check_budget(budget, task_size)
        if eff_status == BudgetStatus.EXHAUSTED:
            return RoutingResult(
                selected_provider=None,
                reason_code=ReasonCode.PROVIDER_EXHAUSTED.value,
                reason_human=f"codex-subscription exhausted (size={task_size})",
                budget_status=eff_status.value,
                budget_snapshot_age_seconds=budget_age,
                context_policy=ctx,
                fallback_candidates=["opencode-zai"] if request.opencode_verified else [],
                human_override_required=True,
                task_category=task_cat,
                task_size=task_size,
            )
        return RoutingResult(
            selected_provider="codex-subscription",
            reason_code=ReasonCode.EXPLICIT_REQUEST.value,
            reason_human="explicit codex-subscription request",
            budget_status=budget.status if budget else BudgetStatus.UNKNOWN.value,
            budget_snapshot_age_seconds=budget_age,
            context_policy=ctx,
            provider_verified=True,
            task_category=task_cat,
            task_size=task_size,
        )

    if engine == "opencode-zai":
        if request.allowed_providers and "opencode-zai" not in request.allowed_providers:
            return RoutingResult(
                selected_provider=None,
                reason_code=ReasonCode.PROFILE_REJECTED.value,
                reason_human="opencode-zai rejected by project profile",
                task_category=task_cat,
                task_size=task_size,
            )
        if not request.opencode_available or not request.opencode_verified:
            return RoutingResult(
                selected_provider=None,
                reason_code=ReasonCode.PROVIDER_UNAVAILABLE.value,
                reason_human="opencode-zai not locally verified",
                fallback_candidates=["codex-subscription"] if request.codex_verified else [],
                task_category=task_cat,
                task_size=task_size,
            )
        eff_status, budget_age, ctx = _check_budget(opencode_budget, task_size)
        if eff_status == BudgetStatus.EXHAUSTED:
            return RoutingResult(
                selected_provider=None,
                reason_code=ReasonCode.PROVIDER_EXHAUSTED.value,
                reason_human=f"opencode-zai exhausted (size={task_size})",
                budget_status=eff_status.value,
                budget_snapshot_age_seconds=budget_age,
                context_policy=ctx,
                fallback_candidates=["codex-subscription"] if request.codex_verified else [],
                human_override_required=True,
                task_category=task_cat,
                task_size=task_size,
            )
        return RoutingResult(
            selected_provider="opencode-zai",
            reason_code=ReasonCode.EXPLICIT_REQUEST.value,
            reason_human="explicit opencode-zai request",
            budget_status=opencode_budget.status if opencode_budget else BudgetStatus.UNKNOWN.value,
            budget_snapshot_age_seconds=budget_age,
            context_policy=ctx,
            provider_verified=True,
            task_category=task_cat,
            task_size=task_size,
        )

    # Unknown explicit engine.
    return RoutingResult(
        selected_provider=None,
        reason_code=ReasonCode.PROVIDER_UNAVAILABLE.value,
        reason_human=f"unknown engine: {engine}",
        task_category=task_cat,
        task_size=task_size,
    )


def _check_budget(
    budget: Optional[ProviderBudget],
    task_size: str,
) -> tuple:
    """Check budget status for a task. Returns (effective_status, age, context_policy)."""
    if budget is None:
        return (BudgetStatus.UNKNOWN, None,
                ContextPolicy(label="conservative"))

    # Always classify from remaining_percent, never from stored status.
    eff_status = _classify_status(budget.remaining_percent)
    # Elevate to EXHAUSTED for medium/large when <10%.
    if task_size in ("medium", "large") and budget.remaining_percent is not None:
        if budget.remaining_percent < 10.0:
            eff_status = BudgetStatus.EXHAUSTED

    age = _budget_snapshot_age(budget)
    ctx = _context_policy_for_budget(budget.remaining_percent)
    return (eff_status, age, ctx)


def _route_auto(
    request: RoutingRequest,
    budget: Optional[ProviderBudget],
    opencode_budget: Optional[ProviderBudget],
    task_cat: str,
    task_size: str,
) -> RoutingResult:
    """Automatic GPT-first routing."""
    codex_ok = request.codex_available and request.codex_verified
    opencode_ok = request.opencode_available and request.opencode_verified

    # Project policy filter.
    codex_allowed = (not request.allowed_providers
                     or "codex-subscription" in request.allowed_providers)
    opencode_allowed = (not request.allowed_providers
                        or "opencode-zai" in request.allowed_providers)

    # --- Codex preferred categories ---
    if task_cat in CODEX_PREFERRED_CATEGORIES and codex_ok and codex_allowed:
        eff_status, budget_age, ctx = _check_budget(budget, task_size)
        if eff_status != BudgetStatus.EXHAUSTED:
            return RoutingResult(
                selected_provider="codex-subscription",
                reason_code={
                    "architecture": ReasonCode.GPT_FIRST_ARCHITECTURE.value,
                    "security": ReasonCode.GPT_FIRST_SECURITY.value,
                    "review": ReasonCode.GPT_FIRST_REVIEW.value,
                }.get(task_cat, ReasonCode.GPT_FIRST_DEFAULT.value),
                reason_human=f"GPT-first: {task_cat} routed to codex-subscription",
                budget_status=budget.status if budget else BudgetStatus.UNKNOWN.value,
                budget_snapshot_age_seconds=budget_age,
                context_policy=ctx,
                provider_verified=True,
                fallback_candidates=["opencode-zai"] if opencode_ok and opencode_allowed else [],
                task_category=task_cat,
                task_size=task_size,
            )
        # Codex exhausted for this size. Try GLM.
        if opencode_ok and opencode_allowed:
            oe_status, oe_age, oe_ctx = _check_budget(opencode_budget, task_size)
            if oe_status != BudgetStatus.EXHAUSTED:
                return RoutingResult(
                    selected_provider="opencode-zai",
                    reason_code=ReasonCode.GLM_FALLBACK.value,
                    reason_human=f"codex-subscription exhausted; GLM fallback for {task_cat}",
                    budget_status=oe_status.value,
                    budget_snapshot_age_seconds=oe_age,
                    context_policy=oe_ctx,
                    provider_verified=True,
                    fallback_candidates=[],
                    task_category=task_cat,
                    task_size=task_size,
                )

    # --- Codex default (any category) ---
    if codex_ok and codex_allowed:
        eff_status, budget_age, ctx = _check_budget(budget, task_size)
        if eff_status != BudgetStatus.EXHAUSTED:
            return RoutingResult(
                selected_provider="codex-subscription",
                reason_code=ReasonCode.GPT_FIRST_DEFAULT.value,
                reason_human=f"GPT-first: default to codex-subscription for {task_cat}",
                budget_status=budget.status if budget else BudgetStatus.UNKNOWN.value,
                budget_snapshot_age_seconds=budget_age,
                context_policy=ctx,
                provider_verified=True,
                fallback_candidates=["opencode-zai"] if opencode_ok and opencode_allowed else [],
                task_category=task_cat,
                task_size=task_size,
            )

    # --- GLM for acceptable categories when Codex unavailable ---
    if task_cat in GLM_ACCEPTABLE_CATEGORIES and opencode_ok and opencode_allowed:
        eff_status, budget_age, ctx = _check_budget(opencode_budget, task_size)
        if eff_status != BudgetStatus.EXHAUSTED:
            code = {
                "implementation": ReasonCode.GLM_VERIFIED_IMPLEMENTATION.value,
                "tests": ReasonCode.GLM_VERIFIED_TESTS.value,
                "documentation": ReasonCode.GLM_VERIFIED_DOCUMENTATION.value,
            }.get(task_cat, ReasonCode.GLM_FALLBACK.value)
            return RoutingResult(
                selected_provider="opencode-zai",
                reason_code=code,
                reason_human=f"GLM verified for {task_cat}",
                budget_status=opencode_budget.status if opencode_budget else BudgetStatus.UNKNOWN.value,
                budget_snapshot_age_seconds=budget_age,
                context_policy=ctx,
                provider_verified=True,
                fallback_candidates=["codex-subscription"] if codex_ok else [],
                task_category=task_cat,
                task_size=task_size,
            )

    # --- Codex exhausted, try GLM for any category ---
    if codex_ok and codex_allowed and budget is not None:
        eff_status, _, _ = _check_budget(budget, task_size)
        if eff_status == BudgetStatus.EXHAUSTED:
            if opencode_ok and opencode_allowed:
                oe_status, oe_age, oe_ctx = _check_budget(opencode_budget, task_size)
                if oe_status != BudgetStatus.EXHAUSTED:
                    return RoutingResult(
                        selected_provider="opencode-zai",
                        reason_code=ReasonCode.GLM_FALLBACK.value,
                        reason_human="codex-subscription exhausted; GLM fallback",
                        budget_status=oe_status.value,
                        budget_snapshot_age_seconds=oe_age,
                        context_policy=oe_ctx,
                        provider_verified=True,
                        fallback_candidates=[],
                        task_category=task_cat,
                        task_size=task_size,
                    )

    # --- Neither provider usable ---
    reasons = []
    if not codex_ok:
        reasons.append("codex-subscription not verified")
    elif not codex_allowed:
        reasons.append("codex-subscription blocked by profile")
    if not opencode_ok:
        reasons.append("opencode-zai not verified")
    elif not opencode_allowed:
        reasons.append("opencode-zai blocked by profile")

    return RoutingResult(
        selected_provider=None,
        reason_code=ReasonCode.NO_PROVIDER.value,
        reason_human="; ".join(reasons) if reasons else "no provider available",
        fallback_candidates=[],
        human_override_required=True,
        task_category=task_cat,
        task_size=task_size,
    )
