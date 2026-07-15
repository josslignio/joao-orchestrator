"""Approval domain helpers.

V1.4.1 records human approval decisions as events. No automatic approval is
ever allowed; this module only structures the decision.
"""

from __future__ import annotations

from typing import List, Optional

from .events import make_event


def make_approval_event(task_id: str, decision: str, reviewer: str = "human",
                        note: str = "") -> dict:
    """decision: APPROVED | REJECTED. Appended to the task event log."""
    if decision not in ("APPROVED", "REJECTED"):
        raise ValueError(f"invalid approval decision: {decision}")
    to_state = "APPROVED" if decision == "APPROVED" else "REJECTED"
    return make_event(task_id=task_id, from_state="AWAITING_APPROVAL",
                      to_state=to_state, reason=f"{decision} by {reviewer}",
                      decision=decision, reviewer=reviewer, note=note)
