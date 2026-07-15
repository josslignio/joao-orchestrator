"""Task complexity estimation for routing.

Phase H. Estimates a task's complexity (0-100) from its metadata to inform
provider routing. Higher complexity → prefer stronger providers (Codex/GPT).

Heuristics:
- Category: architecture/security/review score higher than tests/docs.
- Size: large > medium > small.
- Done-criteria count: more criteria → higher complexity.
- Dependencies: more deps → higher complexity (coordinating changes).
- Request length: longer requests → higher complexity.
- Keywords: "refactor", "migration", "performance", "concurrency" bump score.

Deterministic: same inputs → same score. No model calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional


# Category base complexity.
_CATEGORY_BASE = {
    "architecture": 70,
    "security": 75,
    "review": 60,
    "implementation": 40,
    "tests": 25,
    "documentation": 15,
}

# Size multiplier.
_SIZE_MULTIPLIER = {
    "small": 1.0,
    "medium": 1.3,
    "large": 1.6,
}

# Complexity keywords in the request/done-criteria.
_COMPLEXITY_KEYWORDS = {
    "refactor": 15,
    "migration": 20,
    "migrate": 20,
    "performance": 10,
    "optimize": 10,
    "concurrency": 15,
    "thread": 10,
    "async": 8,
    "security": 15,
    "crypto": 15,
    "authentication": 12,
    "authorization": 12,
    "database": 10,
    "schema": 8,
    "api": 5,
    "network": 8,
    "distributed": 15,
    "algorithm": 12,
    "parser": 10,
    "compiler": 20,
}


@dataclass
class ComplexityEstimate:
    """The estimated complexity of a task."""
    score: int = 0            # 0-100
    category: str = ""
    size: str = ""
    base_score: int = 0
    keyword_bonus: int = 0
    criteria_bonus: int = 0
    deps_bonus: int = 0
    length_bonus: int = 0
    factors: List[str] = field(default_factory=list)
    schema_version: int = 1

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "score": self.score,
            "category": self.category,
            "size": self.size,
            "base_score": self.base_score,
            "keyword_bonus": self.keyword_bonus,
            "criteria_bonus": self.criteria_bonus,
            "deps_bonus": self.deps_bonus,
            "length_bonus": self.length_bonus,
            "factors": self.factors,
        }


def estimate_complexity(
    category: str = "implementation",
    size: str = "small",
    done_criteria: Optional[List[str]] = None,
    dependencies: Optional[List[str]] = None,
    request_text: str = "",
) -> ComplexityEstimate:
    """Estimate task complexity. Returns a ComplexityEstimate (0-100 score)."""
    cat = category.lower() if category else "implementation"
    sz = size.lower() if size else "small"
    if cat not in _CATEGORY_BASE:
        cat = "implementation"
    if sz not in _SIZE_MULTIPLIER:
        sz = "small"

    base = _CATEGORY_BASE[cat]
    factors: List[str] = [f"category={cat}({base})"]

    # Size multiplier.
    mult = _SIZE_MULTIPLIER[sz]
    base = int(base * mult)
    factors.append(f"size={sz}(*{mult})")

    estimate = ComplexityEstimate(
        category=cat, size=sz, base_score=base)

    # Keyword bonus.
    text = (request_text or "").lower()
    all_text = text + " " + " ".join(done_criteria or []).lower()
    keyword_bonus = 0
    matched_keywords = []
    for kw, bonus in _COMPLEXITY_KEYWORDS.items():
        if re.search(r"\b" + re.escape(kw) + r"\b", all_text):
            keyword_bonus += bonus
            matched_keywords.append(kw)
    if matched_keywords:
        estimate.keyword_bonus = min(keyword_bonus, 30)
        factors.append(f"keywords={','.join(matched_keywords[:5])}(+{estimate.keyword_bonus})")

    # Criteria count bonus.
    criteria_count = len(done_criteria or [])
    if criteria_count > 0:
        estimate.criteria_bonus = min(criteria_count * 3, 15)
        factors.append(f"criteria={criteria_count}(+{estimate.criteria_bonus})")

    # Dependencies bonus.
    deps_count = len(dependencies or [])
    if deps_count > 0:
        estimate.deps_bonus = min(deps_count * 4, 20)
        factors.append(f"deps={deps_count}(+{estimate.deps_bonus})")

    # Request length bonus.
    if len(request_text) > 500:
        estimate.length_bonus = min(int((len(request_text) - 500) / 100), 15)
        factors.append(f"length={len(request_text)}(+{estimate.length_bonus})")

    estimate.score = min(
        base + estimate.keyword_bonus + estimate.criteria_bonus
        + estimate.deps_bonus + estimate.length_bonus, 100)
    estimate.factors = factors
    return estimate


def complexity_tier(score: int) -> str:
    """Map a complexity score to a tier name for routing."""
    if score >= 70:
        return "high"
    if score >= 40:
        return "medium"
    return "low"
