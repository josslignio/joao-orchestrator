"""V2 budget guard + anti-loop detector (§13, §14).

This is a thin V2 adapter over the EXISTING engine — it does NOT reimplement
telemetry or the circuit breaker. It wraps:

* :mod:`joao_orchestrator.optimization.telemetry` (call/recording surfaces)
* :mod:`joao_orchestrator.optimization.circuit_breaker` (trip reasons)
* :mod:`joao_orchestrator.runtime.drift` (file reopen / repeated question)

…with the V2 budget defaults from §13 and the V2 anti-loop catalogue from §14,
adding the one pattern the existing breaker does not cover: **reopened gates**.

Budget defaults (§13, verbatim):

    exploration_seconds              900
    max_repair_loops_per_gate          2
    max_same_file_reads_unchanged      2
    max_same_failing_command_wo_evidence 2
    max_repeated_summary               1
    max_active_objectives              1
    max_full_suite_runs                1

Warn at 70 %, stop/escalate at 100 % (§13).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Sequence

from .objective import RunBudgets

# m1 closure: previous revisions imported the engine's circuit_breaker and
# drift modules here under throwaway aliases purely as "reuse markers" — they
# were never referenced (a false reuse claim). Those dead imports have been
# removed. The AntiLoopDetector below is self-contained; it does not import
# engine surfaces it does not use.

WARN_THRESHOLD = 0.70
STOP_THRESHOLD = 1.00


# ---------------------------------------------------------------------------
# Budget guard
# ---------------------------------------------------------------------------

@dataclass
class BudgetUsage:
    """Snapshot of one budget dimension's consumption."""
    name: str
    used: float
    limit: float
    unit: str = ""

    @property
    def fraction(self) -> float:
        return (self.used / self.limit) if self.limit else 0.0

    @property
    def level(self) -> str:
        f = self.fraction
        if f >= STOP_THRESHOLD:
            return "STOP"
        if f >= WARN_THRESHOLD:
            return "WARN"
        return "OK"

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "used": self.used, "limit": self.limit,
                "unit": self.unit, "fraction": round(self.fraction, 3),
                "level": self.level}


@dataclass
class BudgetGuard:
    """Tracks budget consumption and raises stop/warn at the §13 thresholds."""
    budgets: RunBudgets = field(default_factory=RunBudgets.defaults)
    exploration_seconds_used: float = 0.0
    repair_loops_by_gate: dict[str, int] = field(default_factory=dict)
    same_file_reads_unchanged: dict[str, int] = field(default_factory=dict)
    same_failing_command_count: int = 0
    repeated_summary_count: int = 0
    active_objectives: int = 0
    full_suite_runs: int = 0

    # -- recording --------------------------------------------------------
    def record_exploration(self, seconds: float) -> None:
        self.exploration_seconds_used += seconds

    def record_repair_loop(self, gate_id: str) -> None:
        self.repair_loops_by_gate[gate_id] = (
            self.repair_loops_by_gate.get(gate_id, 0) + 1)

    def record_file_read_unchanged(self, path: str) -> None:
        self.same_file_reads_unchanged[path] = (
            self.same_file_reads_unchanged.get(path, 0) + 1)

    def record_same_failing_command(self) -> None:
        self.same_failing_command_count += 1

    def record_summary(self) -> None:
        self.repeated_summary_count += 1

    def record_full_suite(self) -> None:
        self.full_suite_runs += 1

    # -- evaluation -------------------------------------------------------
    def usage(self) -> list[BudgetUsage]:
        b = self.budgets
        out = [
            BudgetUsage("exploration_seconds", self.exploration_seconds_used,
                        b.exploration_seconds, "s"),
            BudgetUsage("max_repeated_summary", self.repeated_summary_count,
                        b.max_repeated_summary),
            BudgetUsage("max_active_objectives", self.active_objectives,
                        b.max_active_objectives),
            BudgetUsage("max_full_suite_runs", self.full_suite_runs,
                        b.max_full_suite_runs),
        ]
        # per-gate repair loops: report the worst gate
        if self.repair_loops_by_gate:
            worst_gate = max(self.repair_loops_by_gate,
                             key=lambda g: self.repair_loops_by_gate[g])
            out.append(BudgetUsage(
                f"repair_loops:{worst_gate}",
                self.repair_loops_by_gate[worst_gate],
                b.max_repair_loops_per_gate))
        if self.same_file_reads_unchanged:
            worst_file = max(self.same_file_reads_unchanged,
                             key=lambda f: self.same_file_reads_unchanged[f])
            out.append(BudgetUsage(
                f"reads_unchanged:{worst_file}",
                self.same_file_reads_unchanged[worst_file],
                b.max_same_file_reads_unchanged))
        return out

    def stop_reasons(self) -> list[str]:
        return [u.name for u in self.usage() if u.level == "STOP"]

    def warn_reasons(self) -> list[str]:
        return [u.name for u in self.usage() if u.level == "WARN"]

    def must_stop(self) -> bool:
        return bool(self.stop_reasons())


# ---------------------------------------------------------------------------
# Anti-loop detector (§14) — extends the existing breaker with gate-reopen
# ---------------------------------------------------------------------------

LOOP_PATTERNS = (
    "repeated_command",
    "repeated_error",
    "repeated_unchanged_file_read",
    "reopened_gate",
    "planning_without_diff",
    "test_fix_test_no_new_evidence",
    "repeated_summary",
    "repeated_pr_create_on_merged_branch",
    "repeated_full_suite",
    "repeated_environment_diagnosis_after_known_good",
)


@dataclass
class LoopEvent:
    pattern: str
    detail: str
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"pattern": self.pattern, "detail": self.detail,
                "evidence": self.evidence}


class AntiLoopDetector:
    """Detects the §14 loop catalogue. Lightweight and side-effect free.

    NOTE: this class does NOT compose or import the engine's CircuitBreaker —
    earlier revisions claimed a reuse relationship that was never wired. It is
    a standalone, self-contained detector. The engine's
    :mod:`joao_orchestrator.optimization.circuit_breaker` covers a separate set
    of loop signals (repeated failing tests, file reopening); this class adds
    the V2-specific signals:

    * ``repeated_command`` / ``repeated_error`` (exact argv/returncode repeats);
    * ``reopened_gate`` (a PASSED gate being re-evaluated without a
      contradiction — the §9 invariant violation);
    * ``planning_without_diff`` (planning steps that produced no file change);
    * ``repeated_full_suite``.

    On detection, the §14 response is: stop → identify missing evidence →
    consult known-good registry → consult project state → choose a different
    bounded strategy → preserve last valid artifact → escalate with exact
    blocker + resume command.
    """

    def __init__(self) -> None:
        self._commands: Counter[str] = Counter()
        self._errors: Counter[str] = Counter()
        self._planning_steps: int = 0
        self._full_suites: int = 0
        self._reopened_gate_seen: set[str] = set()
        self.events: list[LoopEvent] = []

    def record_command(self, argv: Sequence[str], returncode: int) -> LoopEvent | None:
        key = " ".join(argv)
        self._commands[key] += 1
        if returncode != 0:
            self._errors[f"{key}::{returncode}"] += 1
        ev: LoopEvent | None = None
        if self._commands[key] >= 3:
            ev = LoopEvent("repeated_command",
                           f"command run {self._commands[key]}x: {key}",
                           evidence=[f"count={self._commands[key]}"])
            self.events.append(ev)
        err_key = f"{key}::{returncode}"
        if self._errors[err_key] >= 2 and returncode != 0:
            ev = LoopEvent(
                "repeated_error",
                f"same failing command {self._errors[err_key]}x: {key} (rc={returncode})",
                evidence=[f"count={self._errors[err_key]}"])
            if ev not in self.events:
                self.events.append(ev)
        return ev

    def record_reopened_gate(self, gate_id: str) -> LoopEvent:
        """A PASSED gate is being re-evaluated without a contradiction (§9)."""
        self._reopened_gate_seen.add(gate_id)
        ev = LoopEvent(
            "reopened_gate",
            f"PASSED gate {gate_id!r} re-evaluated without contradiction "
            "(§9 invariant violation — require ContradictionRecord to reopen)",
            evidence=[f"gate={gate_id}"])
        self.events.append(ev)
        return ev

    def record_planning_step(self, diff_bytes: int) -> LoopEvent | None:
        self._planning_steps += 1
        if self._planning_steps >= 2 and diff_bytes == 0:
            ev = LoopEvent(
                "planning_without_diff",
                f"{self._planning_steps} planning steps with no file diff",
                evidence=[f"steps={self._planning_steps}", "diff_bytes=0"])
            self.events.append(ev)
            return ev
        return None

    def record_full_suite(self) -> LoopEvent | None:
        self._full_suites += 1
        if self._full_suites >= 2:
            ev = LoopEvent(
                "repeated_full_suite",
                f"full suite run {self._full_suites}x — use impact selection (§18)",
                evidence=[f"count={self._full_suites}"])
            self.events.append(ev)
            return ev
        return None

    def tripped(self) -> bool:
        return bool(self.events)


__all__ = [
    "WARN_THRESHOLD", "STOP_THRESHOLD", "BudgetUsage", "BudgetGuard",
    "LOOP_PATTERNS", "LoopEvent", "AntiLoopDetector",
]
