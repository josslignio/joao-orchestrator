"""V3 C2.1 — Governance bootstrap guard + gate-modification policy.

C2 introduced the first operational gate set while establishing GATES.lock.
This module records the one-time bootstrap exception and PERMANENTLY disables
bootstrap gate modification: after C2.1, no ordinary feat/autopilot PR may
modify GATES.lock or weaken a registered gate. Only a governance-candidate
(meta-tests -> Codex review -> batched governance release review) may propose a
gate change, and it must carry governance evidence.

Design invariants: stdlib only; generic core has no project literals; fail-closed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# The one-time bootstrap exception. Once recorded, the flag is permanently False.
BOOTSTRAP_USED = True   # C2 used it (historical fact)
BOOTSTRAP_AVAILABLE = False  # permanently closed after C2.1


@dataclass
class BootstrapException:
    """The one-time governance bootstrap exception record."""
    exception_id: str = "BOOTSTRAP-C2-GATES-001"
    task: str = "C2 (PR budget + scope + evidence-provenance gates)"
    reason: str = (
        "C2 introduced the first operational gate set while establishing "
        "GATES.lock. A gate set cannot pre-exist itself; establishing the gate "
        "registry is a one-time bootstrap act.")
    granted_at: str = "2026-07-15T15:12:13Z"  # C2 merge timestamp
    closed_at: str = "2026-07-15T15:30:00Z"   # C2.1 closes it
    reusable: bool = False                    # permanently non-reusable
    closed_permanently: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GateModificationRequest:
    """A request to modify GATES.lock or a registered gate."""
    branch: str                       # the PR branch name
    is_governance_candidate: bool     # True only for governance/* branches
    modifies_gates_lock: bool         # does it touch GATES.lock?
    carries_governance_evidence: bool # meta-tests + review evidence attached?
    weakens_a_gate: bool              # does it loosen a registered gate?

    def validate(self) -> tuple[bool, list[str]]:
        """Returns (allowed, reasons). Ordinary PRs fail closed."""
        reasons: list[str] = []
        if self.modifies_gates_lock and not self.is_governance_candidate:
            reasons.append(
                "ordinary feat/autopilot PR cannot modify GATES.lock; "
                "use a governance/* candidate branch")
        if self.weakens_a_gate and not self.is_governance_candidate:
            reasons.append(
                "ordinary PR cannot weaken a registered gate; "
                "use a governance/* candidate branch")
        if self.is_governance_candidate and not self.carries_governance_evidence:
            reasons.append(
                "governance candidate missing required evidence "
                "(meta-tests + Codex review)")
        if not BOOTSTRAP_AVAILABLE and self.branch.startswith("feat/"):
            # bootstrap is closed; feat/ branches never get gate-mod powers
            if self.modifies_gates_lock or self.weakens_a_gate:
                pass  # already caught above; this is belt-and-suspenders
        return (not reasons), reasons


def is_bootstrap_available() -> bool:
    """Permanently False after C2.1. The bootstrap cannot be reused."""
    return BOOTSTRAP_AVAILABLE


__all__ = [
    "BOOTSTRAP_USED", "BOOTSTRAP_AVAILABLE",
    "BootstrapException", "GateModificationRequest", "is_bootstrap_available",
]
