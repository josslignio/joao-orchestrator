"""Budget-aware routing extension + review deduplication (T8).

Extends the existing providers/router.py — does NOT replace route(). The
existing router handles provider selection by category/size/budget; this module
adds:

  1. Complexity-tier routing policy (TRIVIAL/SMALL/MEDIUM/COMPLEX/SENSITIVE)
     aligned with the plan compiler (T2).
  2. Review deduplication: skip a Codex review when all bound hashes match a
     prior PASS (uses the T6 content cache).

Spec tier policy:
  TRIVIAL:  deterministic tools/templates; no Codex unless publication/sensitive
  SMALL:    one implementation call; targeted tests; one final Codex review
  MEDIUM:   one implementation call; one clarification batch; one review; one correction
  COMPLEX:  explicit larger budget; one clarification batch; one review; human after 2nd defect
  SENSITIVE: human-only

Rules:
  - no provider fan-out;
  - no automatic retry;
  - no multi-agent voting;
  - no duplicate review when all bound hashes match prior PASS;
  - Codex gets diff + relevant files + gates only;
  - hard review-packet limit;
  - record skip/escalation reasons.

Deterministic. No network. Stdlib only.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Optional

from ..evaluation.models import sha256_json
from .cache import CacheKey, ContentCache
from .plan_compiler import Complexity, ReviewRequirement


SCHEMA_VERSION = 1
REVIEW_PACKET_MAX_BYTES = 24 * 1024  # hard limit per spec


@dataclass(frozen=True)
class RoutingPolicy:
    """The routing policy for a complexity tier."""
    complexity: Complexity
    allow_implementation_provider: bool
    allow_codex_review: bool
    max_clarification_batches: int
    max_corrections: int
    allow_fanout: bool          # always False per spec
    allow_auto_retry: bool      # always False per spec
    human_after_defects: int    # 0 = never; 2 for COMPLEX
    escalate_to_human: bool     # True for SENSITIVE
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "complexity": self.complexity.value,
            "allow_implementation_provider": self.allow_implementation_provider,
            "allow_codex_review": self.allow_codex_review,
            "max_clarification_batches": self.max_clarification_batches,
            "max_corrections": self.max_corrections,
            "allow_fanout": self.allow_fanout,
            "allow_auto_retry": self.allow_auto_retry,
            "human_after_defects": self.human_after_defects,
            "escalate_to_human": self.escalate_to_human,
            "reason": self.reason,
        }


_POLICIES: dict[Complexity, RoutingPolicy] = {
    Complexity.TRIVIAL: RoutingPolicy(
        complexity=Complexity.TRIVIAL,
        allow_implementation_provider=True,
        allow_codex_review=False,
        max_clarification_batches=0, max_corrections=0,
        allow_fanout=False, allow_auto_retry=False,
        human_after_defects=0, escalate_to_human=False,
        reason="trivial: deterministic tools/templates; no Codex unless boundary",
    ),
    Complexity.SMALL: RoutingPolicy(
        complexity=Complexity.SMALL,
        allow_implementation_provider=True,
        allow_codex_review=True,
        max_clarification_batches=0, max_corrections=1,
        allow_fanout=False, allow_auto_retry=False,
        human_after_defects=0, escalate_to_human=False,
        reason="small: one impl call; targeted tests; one final Codex review",
    ),
    Complexity.MEDIUM: RoutingPolicy(
        complexity=Complexity.MEDIUM,
        allow_implementation_provider=True,
        allow_codex_review=True,
        max_clarification_batches=1, max_corrections=1,
        allow_fanout=False, allow_auto_retry=False,
        human_after_defects=0, escalate_to_human=False,
        reason="medium: one impl; one clarification; one review; one correction",
    ),
    Complexity.COMPLEX: RoutingPolicy(
        complexity=Complexity.COMPLEX,
        allow_implementation_provider=True,
        allow_codex_review=True,
        max_clarification_batches=1, max_corrections=2,
        allow_fanout=False, allow_auto_retry=False,
        human_after_defects=2, escalate_to_human=False,
        reason="complex: larger budget; human after second unresolved defect",
    ),
    Complexity.SENSITIVE: RoutingPolicy(
        complexity=Complexity.SENSITIVE,
        allow_implementation_provider=False,
        allow_codex_review=False,
        max_clarification_batches=0, max_corrections=0,
        allow_fanout=False, allow_auto_retry=False,
        human_after_defects=0, escalate_to_human=True,
        reason="sensitive: human-only; no provider implementation",
    ),
}


def policy_for(complexity: Complexity) -> RoutingPolicy:
    return _POLICIES[complexity]


@dataclass(frozen=True)
class ReviewDedupKey:
    """The bound hashes that determine review uniqueness.

    If all bound hashes match a prior PASS review, the review is skipped
    (deduplicated). Any hash change invalidates the prior PASS.
    """
    plan_hash: str
    diff_hash: str
    relevant_source_hash: str
    relevant_test_hash: str
    context_hash: str
    review_criteria_hash: str
    provider_model: str = ""
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plan_hash": self.plan_hash,
            "diff_hash": self.diff_hash,
            "relevant_source_hash": self.relevant_source_hash,
            "relevant_test_hash": self.relevant_test_hash,
            "context_hash": self.context_hash,
            "review_criteria_hash": self.review_criteria_hash,
            "provider_model": self.provider_model,
        }

    @property
    def digest(self) -> str:
        return sha256_json(self.to_dict())


@dataclass(frozen=True)
class ReviewDecision:
    """Decision on whether to run a Codex review."""
    run_review: bool
    deduplicated: bool            # True = skipped because prior PASS matches
    prior_verdict: str = ""       # "PASS" if deduplicated
    reason: str = ""
    packet_bytes: int = 0
    packet_within_limit: bool = True
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_review": self.run_review,
            "deduplicated": self.deduplicated,
            "prior_verdict": self.prior_verdict,
            "reason": self.reason,
            "packet_bytes": self.packet_bytes,
            "packet_within_limit": self.packet_within_limit,
        }


def review_packet_within_limit(packet_bytes: int) -> bool:
    """Enforce the hard review-packet byte limit."""
    return packet_bytes <= REVIEW_PACKET_MAX_BYTES


def decide_review(
    key: ReviewDedupKey,
    complexity: Complexity,
    cache: ContentCache,
    packet_bytes: int = 0,
) -> ReviewDecision:
    """Decide whether to run a Codex review for a checkpoint.

    Deduplication: if a prior PASS review exists for the exact bound hashes,
    skip the review. Any hash change (diff, source, test, context, criteria)
    invalidates the prior PASS and forces a fresh review.

    Tier rules:
      - SENSITIVE: no Codex review (human-only).
      - TRIVIAL: no Codex review (unless caller overrides by passing a
        publication/sensitive boundary — handled by the caller).
    """
    policy = policy_for(complexity)

    # Sensitive: never run Codex.
    if not policy.allow_codex_review:
        return ReviewDecision(
            run_review=False, deduplicated=False,
            reason=f"{complexity.value}: codex review not allowed by tier policy",
            packet_bytes=packet_bytes,
            packet_within_limit=review_packet_within_limit(packet_bytes),
        )

    # Packet size limit.
    within = review_packet_within_limit(packet_bytes)
    if not within:
        return ReviewDecision(
            run_review=False, deduplicated=False,
            reason=f"review packet {packet_bytes}B exceeds limit {REVIEW_PACKET_MAX_BYTES}B",
            packet_bytes=packet_bytes, packet_within_limit=False,
        )

    # Deduplication: check cache for a prior PASS.
    cache_key = CacheKey(
        project_id="review-dedup",
        base_commit="",
        plan_hash=key.plan_hash,
        source_hashes=(),
        test_hashes=(),
        argv=(key.digest,),
        context_hash=key.context_hash,
        review_criteria_hash=key.review_criteria_hash,
        provider_model=key.provider_model,
        kind="review_verdict",
    )
    prior = cache.get(cache_key)
    if prior is not None and prior.get("verdict") == "PASS":
        return ReviewDecision(
            run_review=False, deduplicated=True,
            prior_verdict="PASS",
            reason="all bound hashes match prior PASS; review deduplicated",
            packet_bytes=packet_bytes, packet_within_limit=True,
        )

    return ReviewDecision(
        run_review=True, deduplicated=False,
        reason="no matching prior PASS; review required",
        packet_bytes=packet_bytes, packet_within_limit=True,
    )


def record_review_verdict(
    key: ReviewDedupKey,
    verdict: str,
    cache: ContentCache,
    findings_count: int = 0,
) -> None:
    """Record a review verdict so future identical checkpoints can dedup.

    Only PASS verdicts are cached (per spec: never cache failed/low-confidence).
    """
    if verdict != "PASS":
        return  # never cache non-PASS
    cache_key = CacheKey(
        project_id="review-dedup",
        base_commit="",
        plan_hash=key.plan_hash,
        source_hashes=(),
        test_hashes=(),
        argv=(key.digest,),
        context_hash=key.context_hash,
        review_criteria_hash=key.review_criteria_hash,
        provider_model=key.provider_model,
        kind="review_verdict",
    )
    cache.put(cache_key, {"verdict": "PASS", "findings_count": findings_count})
