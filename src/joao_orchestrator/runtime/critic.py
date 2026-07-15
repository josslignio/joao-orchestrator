"""Review critic — strict PASS/FAIL/FIX gate with one fix attempt max.

Phase D. A focused policy layer over the convergence review engine.

Key guarantees:
- The critic returns EXACTLY one of three verdicts: PASS, FAIL, FIX.
- PASS: the change is approved at the review level (but NEVER owns human
  approval — the task still transitions to AWAITING_APPROVAL).
- FAIL: the change is rejected (blocked, security issue, or fix failed).
- FIX: one fix attempt is allowed; after the fix, the critic re-reviews and
  returns either PASS or FAIL. No second fix.
- Maximum one fix attempt per task. Enforced.
- The critic NEVER approves (never transitions to APPROVED). Only AWAITING_APPROVAL.

Design:
- Builds on the existing ReviewResult schema (verdict, findings).
- Maps the convergence ReviewVerdict (PASS/FIX_REQUIRED/BLOCKED) to the
  critic's three-verdict space.
- Enforces the one-fix invariant via a critic_state.json artifact.
- Offline-deterministic and testable with fake reviewer/fixer scripts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..domain.events import now_iso
from ..storage.atomic import atomic_write_json, append_line
from .convergence import (
    ReviewResult, ReviewVerdict, ReviewFinding, ConvergenceResult,
    ConvergenceConfig, run_convergence, build_review_context,
)


# ---------------------------------------------------------------------------
# Critic verdict (strict three-way)
# ---------------------------------------------------------------------------

class CriticVerdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    FIX = "FIX"


# ---------------------------------------------------------------------------
# Critic state — persisted per task to enforce one-fix invariant
# ---------------------------------------------------------------------------

@dataclass
class CriticState:
    """Per-task critic state. Persisted to enforce the one-fix invariant."""
    schema_version: int = 1
    task_id: str = ""
    project_id: str = ""
    fix_attempts: int = 0
    max_fix_attempts: int = 1
    final_verdict: Optional[str] = None  # PASS or FAIL after completion
    verdict_history: List[dict] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "project_id": self.project_id,
            "fix_attempts": self.fix_attempts,
            "max_fix_attempts": self.max_fix_attempts,
            "final_verdict": self.final_verdict,
            "verdict_history": self.verdict_history,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CriticState":
        return cls(
            schema_version=int(d.get("schema_version", 1)),
            task_id=d.get("task_id", ""),
            project_id=d.get("project_id", ""),
            fix_attempts=int(d.get("fix_attempts", 0)),
            max_fix_attempts=int(d.get("max_fix_attempts", 1)),
            final_verdict=d.get("final_verdict"),
            verdict_history=list(d.get("verdict_history", [])),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
        )

    @property
    def fix_available(self) -> bool:
        """True if a fix attempt is still available."""
        return self.fix_attempts < self.max_fix_attempts


# ---------------------------------------------------------------------------
# Critic result
# ---------------------------------------------------------------------------

@dataclass
class CriticResult:
    """The outcome of a critic evaluation."""
    verdict: str = CriticVerdict.FAIL.value
    summary: str = ""
    findings: List[dict] = field(default_factory=list)
    fix_attempts: int = 0
    fix_used: bool = False
    reason_code: str = ""
    final_state: str = ""  # AWAITING_APPROVAL or FAILED
    schema_version: int = 1

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "verdict": self.verdict,
            "summary": self.summary,
            "findings": self.findings,
            "fix_attempts": self.fix_attempts,
            "fix_used": self.fix_used,
            "reason_code": self.reason_code,
            "final_state": self.final_state,
        }

    @property
    def passed(self) -> bool:
        return self.verdict == CriticVerdict.PASS.value


# ---------------------------------------------------------------------------
# Critic engine
# ---------------------------------------------------------------------------

class CriticEngine:
    """Strict review critic with a one-fix-attempt invariant.

    The critic wraps the convergence engine but enforces:
    - max_fixes is ALWAYS 1 (one fix attempt maximum).
    - The verdict space is collapsed to PASS / FAIL / FIX.
    - The critic NEVER transitions to APPROVED. PASS → AWAITING_APPROVAL.

    State is persisted per task in ``critic_state.json`` to prevent a
    second fix attempt across invocations.
    """

    CRITIC_ARTIFACT = "critic_state.json"

    def __init__(self, task_store: Any, now_fn=None):
        self.task_store = task_store
        self.now_fn = now_fn or now_iso

    # -- State persistence ------------------------------------------------- #

    def _state_path(self, project_id: str, task_id: str) -> Path:
        task_root = self.task_store.task_directory(project_id, task_id)
        return task_root / self.CRITIC_ARTIFACT

    def _load_state(self, project_id: str, task_id: str) -> CriticState:
        path = self._state_path(project_id, task_id)
        if not path.is_file():
            return CriticState(
                task_id=task_id, project_id=project_id,
                max_fix_attempts=1,
                created_at=self.now_fn(),
                updated_at=self.now_fn())
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return CriticState.from_dict(data)
        except (json.JSONDecodeError, OSError):
            return CriticState(
                task_id=task_id, project_id=project_id,
                max_fix_attempts=1,
                created_at=self.now_fn(),
                updated_at=self.now_fn())

    def _save_state(self, state: CriticState) -> None:
        path = self._state_path(state.project_id, state.task_id)
        state.updated_at = self.now_fn()
        atomic_write_json(path, state.to_dict())

    def _append_history(self, state: CriticState,
                        verdict: str, summary: str) -> None:
        state.verdict_history.append({
            "verdict": verdict,
            "summary": summary,
            "ts": self.now_fn(),
        })

    # -- Verdict mapping --------------------------------------------------- #

    @staticmethod
    def _map_convergence_verdict(conv: ConvergenceResult) -> str:
        """Map a ConvergenceResult to a critic verdict (PASS/FAIL/FIX)."""
        if conv.ok:
            return CriticVerdict.PASS.value
        # Not ok. Distinguish FIX (fixable) from FAIL (terminal).
        if conv.verdict == ReviewVerdict.FIX_REQUIRED.value:
            return CriticVerdict.FIX.value
        # BLOCKED, error, or failed fix → FAIL.
        return CriticVerdict.FAIL.value

    # -- Public API -------------------------------------------------------- #

    def evaluate(self, task: Any, profile: Any, budget_store: Any,
                 worktree_path: Path,
                 reviewer_executable: Optional[str] = None,
                 fixer_executable: Optional[str] = None,
                 codex_available: bool = False,
                 codex_verified: bool = False,
                 opencode_available: bool = False,
                 opencode_verified: bool = False,
                 ) -> CriticResult:
        """Run the critic on a task.

        Runs the convergence review. If the result is FIX and a fix attempt
        is available, runs the fixer and re-reviews once. After that, the
        verdict is terminal (PASS or FAIL).

        Returns a CriticResult. NEVER transitions to APPROVED.
        """
        state = self._load_state(task.project_id, task.task_id)
        # Enforce the one-fix invariant: max_fixes is always 1.
        # If the state shows a fix was already used and we're re-entering,
        # set max_fixes=0 to prevent a second fix.
        fixes_available = state.max_fix_attempts - state.fix_attempts
        config = ConvergenceConfig(
            max_fixes=min(fixes_available, 1),
            review_engine="auto",
            fix_engine="auto",
            review_executable=reviewer_executable,
            fix_executable=fixer_executable,
            now_fn=self.now_fn,
        )

        # Run convergence.
        conv = run_convergence(
            task, profile, self.task_store, budget_store,
            config, worktree_path,
            codex_available=codex_available,
            codex_verified=codex_verified,
            opencode_available=opencode_available,
            opencode_verified=opencode_verified,
        )

        mapped = self._map_convergence_verdict(conv)

        # Track fix usage.
        fix_used = conv.fixer_calls > 0
        if fix_used:
            state.fix_attempts += conv.fixer_calls

        self._append_history(state, mapped, conv.error or conv.reason_code
                             or "ok")

        result = CriticResult(
            verdict=mapped,
            summary=conv.error or conv.reason_code or "review complete",
            findings=[],  # Populated below from review artifacts if present.
            fix_attempts=state.fix_attempts,
            fix_used=fix_used,
            reason_code=conv.reason_code,
            final_state=conv.final_state,
        )

        # Load findings from the review result artifact if available.
        try:
            task_root = self.task_store.task_directory(
                task.project_id, task.task_id)
            review_path = task_root / "review_result.json"
            if review_path.is_file():
                rr = ReviewResult.from_dict(
                    json.loads(review_path.read_text(encoding="utf-8")))
                result.findings = rr.findings
                if not result.summary or result.summary == "review complete":
                    result.summary = rr.summary
        except Exception:
            pass

        # Determine final verdict.
        if mapped == CriticVerdict.PASS.value:
            state.final_verdict = CriticVerdict.PASS.value
            result.final_state = "AWAITING_APPROVAL"
        else:
            state.final_verdict = CriticVerdict.FAIL.value
            result.final_state = "FAILED"

        self._save_state(state)
        return result

    def evaluate_review(self, review: ReviewResult,
                        project_id: str, task_id: str) -> CriticResult:
        """Evaluate a pre-computed ReviewResult without running convergence.

        Maps the review verdict to the critic's three-way space. Does NOT
        run a fixer — use ``evaluate`` for the full flow.
        """
        state = self._load_state(project_id, task_id)
        if review.verdict == ReviewVerdict.PASS.value:
            mapped = CriticVerdict.PASS.value
        elif review.verdict == ReviewVerdict.BLOCKED.value:
            mapped = CriticVerdict.FAIL.value
        elif review.verdict == ReviewVerdict.FIX_REQUIRED.value:
            # FIX if a fix is available, else FAIL.
            mapped = (CriticVerdict.FIX.value if state.fix_available
                      else CriticVerdict.FAIL.value)
        else:
            mapped = CriticVerdict.FAIL.value

        self._append_history(state, mapped, review.summary)
        result = CriticResult(
            verdict=mapped,
            summary=review.summary,
            findings=review.findings,
            fix_attempts=state.fix_attempts,
            fix_used=state.fix_attempts > 0,
            reason_code=review.reason_code,
            final_state=("AWAITING_APPROVAL" if mapped == CriticVerdict.PASS.value
                         else "FAILED"),
        )
        if mapped == CriticVerdict.PASS.value:
            state.final_verdict = CriticVerdict.PASS.value
        else:
            state.final_verdict = CriticVerdict.FAIL.value
        self._save_state(state)
        return result

    # -- Invariant checks -------------------------------------------------- #

    def assert_no_approval(self, task: Any) -> bool:
        """Verify the critic never approved the task.

        Returns True if the task is NOT in APPROVED state (correct behavior).
        """
        meta = self.task_store.load(task.project_id, task.task_id)
        return meta.state != "APPROVED"

    def fix_attempts_used(self, project_id: str, task_id: str) -> int:
        """Return the number of fix attempts used for a task."""
        state = self._load_state(project_id, task_id)
        return state.fix_attempts
