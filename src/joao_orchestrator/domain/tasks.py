"""Task domain helpers: ID generation, size classification, done-criteria.

No I/O. Pure functions.
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone


# --------------------------------------------------------------------------- #
# IDs
# --------------------------------------------------------------------------- #

def new_task_id() -> str:
    """Short, collision-resistant id: YYYYMMDDHHMMSS + 6 hex chars."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"{stamp}_{secrets.token_hex(3)}"


def slugify(text: str, limit: int = 32) -> str:
    """Make a filesystem/branch-safe slug from free text."""
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", (text or "").strip().lower()).strip("-")
    s = re.sub(r"-+", "-", s)
    return (s or "task")[:limit].rstrip("-")


# --------------------------------------------------------------------------- #
# Size classification (V0.2 planner scaling rules live here as data)
# --------------------------------------------------------------------------- #

SIZE_CLASSES = ("TRIVIAL", "SMALL", "MEDIUM", "LARGE", "HIGH_RISK")


# Per-class defaults. V0.2 will make these configurable per profile.
SIZE_BUDGETS = {
    "TRIVIAL": {"coders": 1, "reviewers": 0, "max_retries": 1, "max_diff_lines": 60},
    "SMALL":   {"coders": 1, "reviewers": 1, "max_retries": 2, "max_diff_lines": 200},
    "MEDIUM":  {"coders": 1, "reviewers": 1, "max_retries": 2, "max_diff_lines": 600},
    "LARGE":   {"coders": 2, "reviewers": 1, "max_retries": 1, "max_diff_lines": 1500},
    "HIGH_RISK": {"coders": 1, "reviewers": 2, "max_retries": 0, "max_diff_lines": 400},
}


def classify_size(request: str, expected_changed_paths=None) -> str:
    """Deterministic first-pass size classification.

    Heuristic: HIGH_RISK/LARGE keywords escalate; otherwise default SMALL.
    V0.2's planner may refine this.
    """
    low = (request or "").lower()
    high_risk_kw = ("security", "auth", "credentials", "migration", "database",
                    "drop", "delete data", "refund", "payment")
    large_kw = ("refactor", "rewrite", "architecture", "multi-file", "across modules")
    if any(k in low for k in high_risk_kw):
        return "HIGH_RISK"
    if any(k in low for k in large_kw):
        return "LARGE"
    n = len(expected_changed_paths or [])
    if n <= 1:
        return "TRIVIAL"
    if n <= 3:
        return "SMALL"
    return "MEDIUM"


# --------------------------------------------------------------------------- #
# Done criteria
# --------------------------------------------------------------------------- #

VAGUE_CRITERIA = (
    "improve code", "make it better", "optimize generally", "clean up",
    "refactor for clarity", "tidy",
)


def is_vague_criteria(criteria: str) -> bool:
    low = (criteria or "").strip().lower()
    if not low:
        return True
    return any(v in low for v in VAGUE_CRITERIA)


def validate_done_criteria(criteria: str, exploratory: bool = False) -> tuple:
    """Return (ok, reason). Non-exploratory coding tasks need verifiable criteria."""
    if exploratory:
        return True, "exploratory task; criteria waived"
    if not criteria or not criteria.strip():
        return False, "missing done criteria (non-exploratory coding task)"
    if is_vague_criteria(criteria):
        return False, f"vague done criteria rejected: {criteria!r}"
    return True, "ok"
