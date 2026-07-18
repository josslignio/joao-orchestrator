"""B-29.1 — COST CASCADE + BEST-OF-N VÉRIFIÉ.

Route each task through escalating tiers, cheapest first, and only pay for more when the
cheaper tier cannot be VERIFIED to have solved it:

    (a) DETERMINISTIC  — a tool/script that already exists (cost ~0, always preferred)
    (b) GLM_SOLO       — one cheap GLM builder
    (c) BEST_OF_N      — N GLM builders on DIVERSE angles, judged (objective tests first,
                         LLM judge only to break a tie tests can't) — builder ≠ judge
    (d) CLAUDE         — the expensive deep model, last resort

Escalation is EXPLICIT and FAIL-CLOSED: a tier that fails its objective test, or a task
tagged critical / carrying a recurrence / coming off a gate failure, escalates DOWN the
cascade — it is never silently accepted. Every routing decision and its cost is logged so
the cost per mission is visible in the evidence.

Workers are injected callables, so this orchestrator is deterministic and unit-testable
with no real model call. The winning artifact still passes the normal independent review
downstream (RunRuntime) — the cascade only decides WHO builds and proves it objectively.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

# Nominal cost units (relative; GLM cheap, Claude expensive) — used for the cost comparison.
COST_DETERMINISTIC = 0.0
COST_GLM = 1.0
COST_CLAUDE = 20.0
COST_JUDGE_LLM = 1.0  # a cheap independent judge, only spent on a real tie

DEFAULT_ANGLES = ("performance", "readability", "edge-cases")


class Tier:
    DETERMINISTIC = "deterministic"
    GLM_SOLO = "glm_solo"
    BEST_OF_N = "best_of_n"
    CLAUDE = "claude"
    BLOCKED = "blocked"


@dataclass
class WorkerResult:
    provider: str
    text: str = ""
    angle: str = ""
    cost: float = 0.0
    ok: bool = True            # the worker actually ran
    tests_passed: bool = False  # objective verification result
    score: float = 0.0          # objective score (e.g. fraction of checks passed)

    def to_dict(self) -> dict:
        return {"provider": self.provider, "angle": self.angle, "cost": self.cost,
                "ok": self.ok, "tests_passed": self.tests_passed, "score": self.score}


@dataclass
class RouteDecision:
    tier: str
    reason: str
    cost: float
    escalated: bool = False

    def to_dict(self) -> dict:
        return {"tier": self.tier, "reason": self.reason, "cost": self.cost, "escalated": self.escalated}


@dataclass
class TaskSpec:
    task_id: str
    prompt: str = ""
    critical: bool = False
    recurrence: bool = False
    gate_failed: bool = False
    tags: list[str] = field(default_factory=list)

    def needs_best_of_n(self) -> bool:
        """Explicit escalation criteria for jumping straight to verified best-of-N."""
        return self.critical or self.recurrence or self.gate_failed


@dataclass
class CascadeResult:
    task_id: str
    tier: str
    winner: Optional[WorkerResult]
    decisions: list[RouteDecision] = field(default_factory=list)
    candidates: list[WorkerResult] = field(default_factory=list)
    judge_verdict: Optional[dict] = None
    total_cost: float = 0.0
    blocked: bool = False

    def to_dict(self) -> dict:
        return {"task_id": self.task_id, "tier": self.tier, "blocked": self.blocked,
                "total_cost": self.total_cost,
                "winner": self.winner.to_dict() if self.winner else None,
                "candidates": [c.to_dict() for c in self.candidates],
                "judge_verdict": self.judge_verdict,
                "decisions": [d.to_dict() for d in self.decisions]}


# Injected worker signatures:
#   deterministic: (TaskSpec) -> WorkerResult | None      (None = no tool available)
#   glm:           (TaskSpec, angle: str) -> WorkerResult
#   claude:        (TaskSpec) -> WorkerResult
#   verify:        (TaskSpec, WorkerResult) -> tuple[bool, float]   (objective tests FIRST)
#   judge:         (TaskSpec, list[WorkerResult]) -> tuple[WorkerResult, dict]  (LLM tiebreak)
class Cascade:
    def __init__(self, *, glm: Callable, claude: Callable, verify: Callable,
                 judge: Callable, deterministic: Optional[Callable] = None,
                 n: int = 3, angles: tuple[str, ...] = DEFAULT_ANGLES):
        self.glm = glm
        self.claude = claude
        self.verify = verify
        self.judge = judge
        self.deterministic = deterministic
        self.n = n
        self.angles = angles

    def _verified(self, task: TaskSpec, result: WorkerResult) -> WorkerResult:
        passed, score = self.verify(task, result)
        result.tests_passed = bool(passed)
        result.score = float(score)
        return result

    def route(self, task: TaskSpec) -> CascadeResult:
        res = CascadeResult(task_id=task.task_id, tier=Tier.BLOCKED, winner=None)

        # ── (a) deterministic tool ─────────────────────────────────
        if self.deterministic is not None:
            out = self.deterministic(task)
            if out is not None:
                out = self._verified(task, out)
                res.total_cost += out.cost
                if out.ok and out.tests_passed:
                    res.decisions.append(RouteDecision(Tier.DETERMINISTIC, "deterministic tool solved it", out.cost))
                    res.tier, res.winner = Tier.DETERMINISTIC, out
                    return res
                res.decisions.append(RouteDecision(Tier.DETERMINISTIC, "tool failed verification → escalate", out.cost, escalated=True))

        # ── (b) GLM solo — skipped for critical/recurrence/gate-failure ──
        if not task.needs_best_of_n():
            out = self._verified(task, self.glm(task, ""))
            res.total_cost += out.cost
            if out.ok and out.tests_passed:
                res.decisions.append(RouteDecision(Tier.GLM_SOLO, "GLM solo verified", out.cost))
                res.tier, res.winner = Tier.GLM_SOLO, out
                return res
            res.decisions.append(RouteDecision(Tier.GLM_SOLO, "GLM solo failed verification → escalate", out.cost, escalated=True))
        else:
            why = [k for k, v in (("critical", task.critical), ("recurrence", task.recurrence),
                                  ("gate_failure", task.gate_failed)) if v]
            res.decisions.append(RouteDecision(Tier.GLM_SOLO, "skipped GLM solo (" + ",".join(why) + ") → best-of-N", 0.0, escalated=True))

        # ── (c) best-of-N GLM, diverse angles, verified ───────────
        winner, verdict, cost, cands = self._best_of_n(task)
        res.candidates = cands
        res.judge_verdict = verdict
        res.total_cost += cost
        if winner is not None and winner.tests_passed:
            res.decisions.append(RouteDecision(Tier.BEST_OF_N, verdict.get("reason", "best-of-N winner verified"), cost))
            res.tier, res.winner = Tier.BEST_OF_N, winner
            return res
        res.decisions.append(RouteDecision(Tier.BEST_OF_N, "no best-of-N candidate passed → escalate to Claude", cost, escalated=True))

        # ── (d) Claude — last resort ──────────────────────────────
        out = self._verified(task, self.claude(task))
        res.total_cost += out.cost
        if out.ok and out.tests_passed:
            res.decisions.append(RouteDecision(Tier.CLAUDE, "Claude verified", out.cost))
            res.tier, res.winner = Tier.CLAUDE, out
            return res

        # fail-closed: even Claude could not be verified → BLOCK, never silently accept
        res.decisions.append(RouteDecision(Tier.CLAUDE, "Claude failed verification → BLOCKED (fail-closed)", out.cost, escalated=True))
        res.tier, res.winner, res.blocked = Tier.BLOCKED, None, True
        return res

    def _best_of_n(self, task: TaskSpec):
        """N GLM workers on diverse angles → objective tests first, LLM judge only on a tie."""
        candidates = [self._verified(task, self.glm(task, self.angles[i % len(self.angles)]))
                      for i in range(self.n)]
        cost = sum(c.cost for c in candidates)
        passing = [c for c in candidates if c.tests_passed]
        if not passing:
            return None, {"method": "objective_tests", "reason": "no candidate passed the objective tests",
                          "winner": None}, cost, candidates
        top = max(c.score for c in passing)
        best = [c for c in passing if c.score == top]
        if len(best) == 1:
            # objective tests alone decided it — NO LLM judge spent
            w = best[0]
            return w, {"method": "objective_tests", "reason": f"unique top score {top} on {w.angle}",
                       "winner": w.provider, "angle": w.angle, "judge_cost": 0.0}, cost, candidates
        # genuine tie the tests cannot break → the independent LLM judge decides (builder ≠ judge)
        winner, verdict = self.judge(task, best)
        verdict = {"method": "llm_judge_tiebreak", "judge_cost": COST_JUDGE_LLM,
                   "tied_angles": [c.angle for c in best], **(verdict or {})}
        cost += COST_JUDGE_LLM
        return winner, verdict, cost, candidates


@dataclass
class MissionCost:
    """Per-mission routing/cost ledger — makes the cost per mission visible in the evidence."""
    results: list[CascadeResult] = field(default_factory=list)

    def record(self, result: CascadeResult) -> None:
        self.results.append(result)

    def total(self) -> float:
        return sum(r.total_cost for r in self.results)

    def by_tier(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for r in self.results:
            out[r.tier] = out.get(r.tier, 0.0) + r.total_cost
        return out

    def report(self) -> dict:
        return {"total_cost": self.total(), "by_tier": self.by_tier(),
                "tasks": [r.to_dict() for r in self.results]}
