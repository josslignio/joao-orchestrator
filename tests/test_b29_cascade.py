"""B-29.1 — GATE 6: cost cascade + best-of-N verified routing.

Real demo (deterministic, injected workers): a simple task settles in the cheap tier at
minimum cost; a critical task runs best-of-3 with three outputs, a judge verdict, and a
visible cost comparison. Escalation is explicit and fail-closed.
"""
from __future__ import annotations

from joao_orchestrator.providers.cascade import (COST_CLAUDE, COST_GLM, Cascade, MissionCost,
                                                 TaskSpec, Tier, WorkerResult)


def glm(task, angle):
    return WorkerResult(provider="glm", angle=angle, cost=COST_GLM, text=f"glm[{angle}]:{task.task_id}")


def claude(task):
    return WorkerResult(provider="claude", cost=COST_CLAUDE, text=f"claude:{task.task_id}")


def tool(task):
    return WorkerResult(provider="deterministic-tool", cost=0.0, text=f"tool:{task.task_id}")


class Judge:
    """A separate judge (builder ≠ judge). Records how often it is actually consulted."""
    def __init__(self):
        self.calls = 0
    def __call__(self, task, candidates):
        self.calls += 1
        return candidates[0], {"reason": "judge broke the tie", "judge": "independent"}


def _cascade(verify, *, judge=None, deterministic=None):
    return Cascade(glm=glm, claude=claude, verify=verify, judge=judge or Judge(),
                   deterministic=deterministic)


def test_simple_task_uses_deterministic_tier_at_zero_cost():
    c = _cascade(lambda t, r: (True, 1.0), deterministic=tool)
    res = c.route(TaskSpec("simple", prompt="format a file"))
    assert res.tier == Tier.DETERMINISTIC
    assert res.total_cost == 0.0
    assert res.winner.provider == "deterministic-tool"


def test_simple_task_settles_in_glm_solo_at_min_cost():
    c = _cascade(lambda t, r: (True, 1.0))  # no deterministic tool
    res = c.route(TaskSpec("simple2"))
    assert res.tier == Tier.GLM_SOLO
    assert res.total_cost == COST_GLM
    assert res.winner.provider == "glm"


def test_critical_task_runs_best_of_3_with_three_outputs_and_a_verdict():
    # distinct scores per angle so objective tests pick a unique winner
    scores = {"performance": 0.7, "readability": 0.9, "edge-cases": 0.8}
    judge = Judge()
    c = _cascade(lambda t, r: (True, scores[r.angle]), judge=judge)
    res = c.route(TaskSpec("crit", critical=True))
    assert res.tier == Tier.BEST_OF_N
    assert len(res.candidates) == 3                      # three GLM outputs
    assert {x.angle for x in res.candidates} == set(scores)
    assert res.judge_verdict["method"] == "objective_tests"   # tests decided it
    assert judge.calls == 0                              # NO LLM judge needed
    assert res.winner.angle == "readability"            # the top objective score
    assert res.total_cost == 3 * COST_GLM


def test_llm_judge_only_breaks_a_genuine_tie():
    judge = Judge()
    c = _cascade(lambda t, r: (True, 0.9), judge=judge)  # all three tie at 0.9
    res = c.route(TaskSpec("tie", critical=True))
    assert res.judge_verdict["method"] == "llm_judge_tiebreak"
    assert judge.calls == 1                              # judge consulted exactly once
    assert res.winner.provider == "glm"                 # the builder still built it
    assert res.judge_verdict["judge_cost"] == COST_GLM  # the tiebreak cost is visible


def test_builder_is_not_the_judge():
    judge = Judge()
    c = _cascade(lambda t, r: (True, 0.9), judge=judge)
    res = c.route(TaskSpec("sep", critical=True))
    assert judge.calls == 1 and res.winner.provider == "glm"
    assert res.judge_verdict.get("judge") == "independent"  # decided by a distinct judge


def test_glm_solo_failure_escalates_to_best_of_n_explicitly():
    # GLM solo fails verification once, best-of-N then succeeds
    def verify(task, r):
        if r.angle == "":         # the solo call carries no angle
            return (False, 0.0)
        return (True, 0.9)
    c = _cascade(verify)
    res = c.route(TaskSpec("esc"))
    tiers = [d.tier for d in res.decisions]
    assert Tier.GLM_SOLO in tiers and Tier.BEST_OF_N in tiers
    solo = next(d for d in res.decisions if d.tier == Tier.GLM_SOLO)
    assert solo.escalated is True
    assert res.tier == Tier.BEST_OF_N


def test_fail_closed_blocks_when_no_tier_can_be_verified():
    c = _cascade(lambda t, r: (False, 0.0))  # nothing ever verifies
    res = c.route(TaskSpec("hard", critical=True))
    assert res.blocked is True
    assert res.winner is None
    # it must have escalated all the way to Claude before blocking (never silent)
    assert any(d.tier == Tier.CLAUDE for d in res.decisions)
    assert res.total_cost >= COST_CLAUDE
    assert all(d.escalated for d in res.decisions if d.tier in {Tier.GLM_SOLO, Tier.BEST_OF_N, Tier.CLAUDE})


def test_mission_cost_report_compares_cheap_vs_critical():
    # GATE 6 evidence: one cheap task + one critical task, costs compared.
    scores = {"performance": 0.7, "readability": 0.9, "edge-cases": 0.8}
    ledger = MissionCost()
    cheap = _cascade(lambda t, r: (True, 1.0), deterministic=tool)
    crit = _cascade(lambda t, r: (True, scores.get(r.angle, 1.0)))
    ledger.record(cheap.route(TaskSpec("cheap", prompt="rename symbol")))
    ledger.record(crit.route(TaskSpec("critical-auth", critical=True)))
    report = ledger.report()
    assert report["by_tier"][Tier.DETERMINISTIC] == 0.0
    assert report["by_tier"][Tier.BEST_OF_N] == 3 * COST_GLM
    assert report["total_cost"] == 3 * COST_GLM
    # the critical task cost strictly more than the cheap one — the whole point of the cascade
    cheap_cost = report["tasks"][0]["total_cost"]
    crit_cost = report["tasks"][1]["total_cost"]
    assert crit_cost > cheap_cost
