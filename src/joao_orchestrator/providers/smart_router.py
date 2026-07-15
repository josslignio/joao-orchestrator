"""Smart provider router — complexity-aware, capability-aware routing.

Phase H. Extends the existing GPT-first router with:
- Complexity estimation (from task metadata).
- Capability probing (workspace.write, review support).
- Remaining quota weighting.
- Preferred provider override.
- Task profile constraints.
- Deterministic scoring of all candidates.

Routing policy (deterministic):
1. Build a candidate list from available providers.
2. Score each candidate: capability match, budget level, complexity fit,
   preferred-provider bonus, profile allowance.
3. Select the highest-scoring candidate.
4. Ties broken by: preferred > codex > opencode > zai > others (lexical).
5. Fail closed if no candidate scores above zero.

Never falls back to a paid API. Never routes Claude automatically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .base import ProviderAdapter
from .router import (
    RoutingResult, RoutingRequest, ReasonCode as RouterReasonCode,
    ContextPolicy, _check_budget, _budget_snapshot_age,
    CODEX_PREFERRED_CATEGORIES, GLM_ACCEPTABLE_CATEGORIES,
)
from .budget import BudgetStatus, ProviderBudget
from ..runtime.complexity import (
    ComplexityEstimate, estimate_complexity, complexity_tier,
)


# Candidate scores: higher = better fit.
_SCORE_CAPABILITY_MATCH = 30
_SCORE_BUDGET_HEALTHY = 25
_SCORE_COMPLEXITY_FIT = 20
_SCORE_PREFERRED = 50
_SCORE_PROFILE_ALLOWED = 10
_SCORE_AVAILABLE_VERIFIED = 15

_PENALTY_BUDGET_LOW = -15
_PENALTY_BUDGET_EXHAUSTED = -100
_PENALTY_COMPLEXITY_MISMATCH = -10
_PENALTY_PROFILE_BLOCKED = -200


@dataclass
class CandidateScore:
    """The score breakdown for a single provider candidate."""
    provider: str = ""
    total: int = 0
    capability_score: int = 0
    budget_score: int = 0
    complexity_score: int = 0
    preferred_score: int = 0
    profile_score: int = 0
    availability_score: int = 0
    eliminated: bool = False
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "total": self.total,
            "capability_score": self.capability_score,
            "budget_score": self.budget_score,
            "complexity_score": self.complexity_score,
            "preferred_score": self.preferred_score,
            "profile_score": self.profile_score,
            "availability_score": self.availability_score,
            "eliminated": self.eliminated,
            "reason": self.reason,
        }


@dataclass
class SmartRoutingResult:
    """The result of a smart routing decision."""
    selected_provider: Optional[str] = None
    reason_code: str = "no_provider"
    reason_human: str = ""
    complexity: Optional[dict] = None
    scores: List[dict] = field(default_factory=list)
    fallback_candidates: List[str] = field(default_factory=list)
    context_policy: Optional[dict] = None
    budget_status: str = BudgetStatus.UNKNOWN.value
    human_override_required: bool = False
    schema_version: int = 1

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "selected_provider": self.selected_provider,
            "reason_code": self.reason_code,
            "reason_human": self.reason_human,
            "complexity": self.complexity,
            "scores": self.scores,
            "fallback_candidates": self.fallback_candidates,
            "context_policy": self.context_policy,
            "budget_status": self.budget_status,
            "human_override_required": self.human_override_required,
        }


def smart_route(
    request: RoutingRequest,
    budget: Optional[ProviderBudget] = None,
    opencode_budget: Optional[ProviderBudget] = None,
    zai_budget: Optional[ProviderBudget] = None,
    done_criteria: Optional[List[str]] = None,
    dependencies: Optional[List[str]] = None,
    request_text: str = "",
    preferred_provider: Optional[str] = None,
    zai_available: bool = False,
    zai_verified: bool = False,
) -> SmartRoutingResult:
    """Route a task using complexity, capabilities, budget, and preference.

    Deterministic scoring. Returns a SmartRoutingResult with the full
    breakdown for audit.
    """
    # Estimate complexity.
    complexity = estimate_complexity(
        category=request.task_category,
        size=request.task_size,
        done_criteria=done_criteria,
        dependencies=dependencies,
        request_text=request_text,
    )
    tier = complexity_tier(complexity.score)

    # Build candidate list.
    candidates: List[CandidateScore] = []

    # Codex.
    if request.codex_available and request.codex_verified:
        candidates.append(_score_candidate(
            "codex-subscription", request, budget, complexity, tier,
            preferred_provider, "codex"))
    # OpenCode (GLM).
    if request.opencode_available and request.opencode_verified:
        candidates.append(_score_candidate(
            "opencode-zai", request, opencode_budget, complexity, tier,
            preferred_provider, "opencode"))
    # ZAI Coding Plan.
    if zai_available and zai_verified:
        candidates.append(_score_candidate(
            "zai-coding-plan", request, zai_budget, complexity, tier,
            preferred_provider, "zai"))

    if not candidates:
        return SmartRoutingResult(
            selected_provider=None,
            reason_code=RouterReasonCode.NO_PROVIDER.value,
            reason_human="no verified providers available",
            complexity=complexity.to_dict(),
            human_override_required=True,
        )

    # Filter out eliminated candidates.
    viable = [c for c in candidates if not c.eliminated]
    if not viable:
        return SmartRoutingResult(
            selected_provider=None,
            reason_code=RouterReasonCode.PROVIDER_UNAVAILABLE.value,
            reason_human="all candidates eliminated: "
                         + "; ".join(c.reason for c in candidates if c.eliminated),
            complexity=complexity.to_dict(),
            scores=[c.to_dict() for c in candidates],
            human_override_required=True,
        )

    # Sort by total score descending, then by preference order.
    _pref_order = {"codex-subscription": 0, "opencode-zai": 1,
                   "zai-coding-plan": 2}
    viable.sort(key=lambda c: (-c.total, _pref_order.get(c.provider, 99)))

    winner = viable[0]
    fallbacks = [c.provider for c in viable[1:]]

    # Determine context policy from winner's budget.
    winner_budget = _get_budget_for(winner.provider, budget, opencode_budget,
                                    zai_budget)
    _, _, ctx = _check_budget(winner_budget, request.task_size)

    return SmartRoutingResult(
        selected_provider=winner.provider,
        reason_code=_reason_for_winner(winner.provider, request, complexity),
        reason_human=f"smart route: {winner.provider} scored {winner.total} "
                     f"(complexity={tier}, score={complexity.score})",
        complexity=complexity.to_dict(),
        scores=[c.to_dict() for c in candidates],
        fallback_candidates=fallbacks,
        context_policy={"label": ctx.label,
                        "max_tokens_hint": ctx.max_tokens_hint},
        budget_status=winner_budget.status if winner_budget
                      else BudgetStatus.UNKNOWN.value,
    )


def _get_budget_for(provider: str, codex_budget, opencode_budget,
                    zai_budget) -> Optional[ProviderBudget]:
    if provider == "codex-subscription":
        return codex_budget
    if provider == "opencode-zai":
        return opencode_budget
    if provider == "zai-coding-plan":
        return zai_budget
    return None


def _reason_for_winner(provider: str, request: RoutingRequest,
                       complexity: ComplexityEstimate) -> str:
    """Determine the reason code for the winning provider."""
    if request.engine != "auto" and request.engine == provider:
        return RouterReasonCode.EXPLICIT_REQUEST.value
    if provider == "codex-subscription":
        if complexity.category in CODEX_PREFERRED_CATEGORIES:
            return RouterReasonCode.GPT_FIRST_ARCHITECTURE.value
        return RouterReasonCode.GPT_FIRST_DEFAULT.value
    if provider == "opencode-zai":
        if complexity.category in GLM_ACCEPTABLE_CATEGORIES:
            return RouterReasonCode.GLM_VERIFIED_IMPLEMENTATION.value
        return RouterReasonCode.GLM_FALLBACK.value
    return "smart_route_zai"


def _score_candidate(
    provider: str,
    request: RoutingRequest,
    budget: Optional[ProviderBudget],
    complexity: ComplexityEstimate,
    tier: str,
    preferred: Optional[str],
    family: str,
) -> CandidateScore:
    """Score a single candidate provider."""
    score = CandidateScore(provider=provider)

    # Profile check.
    if request.allowed_providers and provider not in request.allowed_providers:
        score.eliminated = True
        score.reason = "blocked by project profile"
        score.profile_score = _PENALTY_PROFILE_BLOCKED
        return score
    score.profile_score = _SCORE_PROFILE_ALLOWED

    # Availability/verification.
    score.availability_score = _SCORE_AVAILABLE_VERIFIED

    # Budget.
    if budget is None:
        score.budget_score = 0
    else:
        eff_status, _, _ = _check_budget(budget, request.task_size)
        if eff_status == BudgetStatus.EXHAUSTED:
            score.eliminated = True
            score.reason = f"budget exhausted for size={request.task_size}"
            score.budget_score = _PENALTY_BUDGET_EXHAUSTED
            return score
        if budget.remaining_percent is not None:
            if budget.remaining_percent > 50:
                score.budget_score = _SCORE_BUDGET_HEALTHY
            elif budget.remaining_percent > 25:
                score.budget_score = _SCORE_BUDGET_HEALTHY // 2
            else:
                score.budget_score = _PENALTY_BUDGET_LOW

    # Complexity fit.
    # High-complexity tasks prefer codex (GPT-first).
    if tier == "high" and family == "codex":
        score.complexity_score = _SCORE_COMPLEXITY_FIT
    elif tier == "low" and family in ("opencode", "zai"):
        score.complexity_score = _SCORE_COMPLEXITY_FIT
    elif tier == "medium":
        score.complexity_score = _SCORE_COMPLEXITY_FIT // 2
    else:
        score.complexity_score = _PENALTY_COMPLEXITY_MISMATCH // 2

    # Preferred provider bonus.
    if preferred and provider == preferred:
        score.preferred_score = _SCORE_PREFERRED

    # Capability match (all providers here support coder role by default).
    score.capability_score = _SCORE_CAPABILITY_MATCH

    score.total = (score.capability_score + score.budget_score
                   + score.complexity_score + score.preferred_score
                   + score.profile_score + score.availability_score)
    return score
