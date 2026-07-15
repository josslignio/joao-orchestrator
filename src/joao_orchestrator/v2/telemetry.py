"""V2 telemetry + sharpness score (§25, §27).

Telemetry (§25) records per-run counters. It reuses the existing
:class:`joao_orchestrator.optimization.telemetry.TelemetryRecorder` for the
low-level recording surface; V2 adds the §25 schema wrapper and the
secret-redaction invariant (no secrets or full prompts in telemetry).

Sharpness score (§27) is a weighted dimensions score. It is explicitly NOT a
proof of safety (§27) — it is a directional improvement signal only.

No secrets, no full prompts (§25).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from ..storage.atomic import atomic_write_json  # reuse
from .resume import RunStore

# Sensitive substrings never allowed in telemetry.
_SENSITIVE_SUBSTRINGS = (
    "token", "secret", "password", "api_key", "apikey", "credential",
    "authorization", "cookie", "session", "bearer",
)


def _redact_value(value: str) -> str:
    """Scrub credential-shaped substrings from a telemetry string value (m6).

    Delegates to :mod:`state`'s scrubber so there is ONE secret-value pattern
    set across V2 (no duplicated regexes / divergent coverage).
    """
    from .state import _redact_value as _scrub  # single source of truth
    return _scrub(value)


def _redact(obj: Any) -> Any:
    if isinstance(obj, Mapping):
        return {k: ("***" if any(s in str(k).lower() for s in _SENSITIVE_SUBSTRINGS)
                    else _redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    if isinstance(obj, str):
        # First scrub credential-shaped values (m6), then truncate long
        # strings that might be prompts/responses.
        scrubbed = _redact_value(obj)
        return (scrubbed if len(scrubbed) <= 200
                else scrubbed[:200] + f"...<+{len(scrubbed)-200}b redacted>")
    return obj


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class RunTelemetry:
    """The §25 per-run telemetry record (no secrets, no full prompts)."""
    run_id: str = ""
    project_id: str = ""
    started_at: str = ""
    finished_at: str = ""
    wall_seconds: float = 0.0
    exploration_seconds: float = 0.0
    model_calls: dict[str, int] = field(default_factory=dict)
    tool_calls: int = 0
    commands: int = 0
    repeated_commands: int = 0
    file_reads: int = 0
    repeated_file_reads: int = 0
    bytes_read: int = 0
    tests_run: int = 0
    test_commands: int = 0
    repair_loops: int = 0
    human_interventions: int = 0
    gates_passed: int = 0
    gates_reopened: int = 0
    visible_artifacts: list[str] = field(default_factory=list)
    blocked_reason: str | None = None
    result: str = "SUCCESS"   # SUCCESS | BLOCKED | INTERRUPTED

    def to_dict(self) -> dict[str, Any]:
        return _redact(asdict(self))

    def write(self, run_store: RunStore) -> None:
        if not self.finished_at:
            self.finished_at = _utcnow()
        run_store.write_telemetry(self.to_dict())


# ---------------------------------------------------------------------------
# Sharpness score (§27)
# ---------------------------------------------------------------------------

SHARPNESS_WEIGHTS = {
    "state_continuity": 0.15,
    "evidence_correctness": 0.15,
    "roadmap_adherence": 0.15,
    "visible_acceptance": 0.15,
    "quota_efficiency": 0.10,
    "test_efficiency": 0.10,
    "recovery": 0.10,
    "loop_avoidance": 0.10,
}


@dataclass
class SharpnessScore:
    """§27 weighted sharpness dimensions. NOT a proof of safety."""
    dimensions: dict[str, float] = field(default_factory=dict)

    @property
    def total(self) -> float:
        return round(sum(self.dimensions.get(k, 0.0) * w
                         for k, w in SHARPNESS_WEIGHTS.items()), 3)

    def to_dict(self) -> dict[str, Any]:
        return {"dimensions": self.dimensions, "weights": SHARPNESS_WEIGHTS,
                "total": self.total,
                "note": "score is not proof of safety (§27)"}


def score_from_telemetry(t: RunTelemetry, *,
                         state_continuity_ok: bool,
                         evidence_correctness_ok: bool,
                         roadmap_adherence_ok: bool,
                         visible_acceptance_ok: bool,
                         recovery_ok: bool) -> SharpnessScore:
    """Derive a §27 score from telemetry + boolean gate signals.

    Each dimension is scored 0..1. The 'efficiency' dimensions come from
    telemetry ratios (fewer repeats/loops/commands = higher score).
    """
    # Quota efficiency: penalize repeated commands/reads and model calls.
    denom = max(t.commands + t.file_reads + 1, 1)
    waste = t.repeated_commands + t.repeated_file_reads + t.repair_loops
    quota = max(0.0, 1.0 - (waste / denom))
    # Test efficiency: few test commands relative to gates passed.
    test_eff = (1.0 if t.gates_passed == 0
                else max(0.0, 1.0 - (t.test_commands / max(t.gates_passed, 1) - 1)))
    test_eff = min(1.0, max(0.0, test_eff))
    # Loop avoidance: 1 minus reopen ratio.
    loops = (1.0 if (t.gates_passed + t.gates_reopened) == 0
             else 1.0 - (t.gates_reopened / (t.gates_passed + t.gates_reopened)))

    return SharpnessScore(dimensions={
        "state_continuity": 1.0 if state_continuity_ok else 0.0,
        "evidence_correctness": 1.0 if evidence_correctness_ok else 0.0,
        "roadmap_adherence": 1.0 if roadmap_adherence_ok else 0.0,
        "visible_acceptance": 1.0 if visible_acceptance_ok else 0.0,
        "quota_efficiency": round(quota, 3),
        "test_efficiency": round(test_eff, 3),
        "recovery": 1.0 if recovery_ok else 0.0,
        "loop_avoidance": round(loops, 3),
    })


# ---------------------------------------------------------------------------
# Benchmark-derived scoring (item 4) — real scores from measured medians.
# ---------------------------------------------------------------------------

def _ratio(baseline: float, v2: float) -> float:
    """Score 0..1 from a baseline-vs-v2 ratio (1.0 = v2 fully eliminates waste)."""
    if baseline <= 0:
        return 1.0 if v2 <= 0 else 0.0
    return max(0.0, min(1.0, 1.0 - (v2 / baseline)))


def score_from_benchmark(
    baseline_median: Mapping[str, float],
    v2_median: Mapping[str, float],
) -> tuple[SharpnessScore, SharpnessScore]:
    """Derive BASELINE and V2 sharpness scores from benchmark medians (item 4).

    M2 closure: scoring is now SYMMETRIC. Both sides use the SAME per-dimension
    formula with the SAME reference scale — there are no baseline-only penalty
    constants (the previous version subtracted 0.3 from baseline and added
    arbitrary +6/+3/2048 constants only to the baseline path, which structurally
    guaranteed V2 would win every dimension). With symmetric scoring, if the
    two sides had identical modeled signatures they would score identically;
    neither side is structurally guaranteed to win. NOT a safety guarantee (§27).
    """
    def pair_for(metric: str) -> tuple[float, float]:
        return baseline_median.get(metric, 0.0), v2_median.get(metric, 0.0)

    # A symmetric scale reference: the worst observed value across BOTH sides
    # for a metric, so each side is scored against the same denominator.
    def sym_ratio(b: float, v: float) -> tuple[float, float]:
        """Score both sides 0..1 by how close they are to the min (best)."""
        lo = min(b, v)
        hi = max(b, v)
        if hi <= 0:
            return 1.0, 1.0   # both zero → both perfect on that metric
        # the side at the min scores 1.0; the worse side scores proportionally.
        b_score = 1.0 if b <= 0 else max(0.0, lo / b) if b >= hi else 1.0
        v_score = 1.0 if v <= 0 else max(0.0, lo / v) if v >= hi else 1.0
        return round(b_score, 3), round(v_score, 3)

    # state_continuity: human_interventions (fewer better) — symmetric.
    b_human, v_human = pair_for("human_interventions")
    base_state, v2_state = sym_ratio(b_human, v_human)
    # evidence_correctness: false_blockers (fewer better) — symmetric.
    b_fb, v_fb = pair_for("false_blockers")
    base_ev, v2_ev = sym_ratio(b_fb, v_fb)
    # roadmap_adherence / visible_acceptance: visible_acceptance_success — both
    # are a 0..1 fraction; score = the fraction itself (symmetric identity).
    b_va, v_va = pair_for("visible_acceptance_success")
    base_road = round(min(1.0, max(0.0, b_va)), 3)
    v2_road = round(min(1.0, max(0.0, v_va)), 3)
    base_vis, v2_vis = base_road, v2_road
    # quota_efficiency: commands — symmetric.
    b_cmd, v_cmd = pair_for("commands")
    base_quota, v2_quota = sym_ratio(b_cmd, v_cmd)
    # test_efficiency: file_reads — symmetric.
    b_fr, v_fr = pair_for("file_reads")
    base_test, v2_test = sym_ratio(b_fr, v_fr)
    # recovery: repair_loops (fewer better) — symmetric.
    b_rec_m, v_rec_m = pair_for("repair_loops")
    base_rec, v2_rec = sym_ratio(b_rec_m, v_rec_m)
    # loop_avoidance: repeated_commands + gate_reopenings — symmetric composite.
    b_rc, v_rc = pair_for("repeated_commands")
    b_gr, v_gr = pair_for("gate_reopenings")
    base_loop, v2_loop = sym_ratio(b_rc + b_gr, v_rc + v_gr)

    baseline_score = SharpnessScore(dimensions={
        "state_continuity": base_state,
        "evidence_correctness": base_ev,
        "roadmap_adherence": base_road,
        "visible_acceptance": base_vis,
        "quota_efficiency": base_quota,
        "test_efficiency": base_test,
        "recovery": base_rec,
        "loop_avoidance": base_loop,
    })
    v2_score = SharpnessScore(dimensions={
        "state_continuity": v2_state,
        "evidence_correctness": v2_ev,
        "roadmap_adherence": v2_road,
        "visible_acceptance": v2_vis,
        "quota_efficiency": v2_quota,
        "test_efficiency": v2_test,
        "recovery": v2_rec,
        "loop_avoidance": v2_loop,
    })
    return baseline_score, v2_score


__all__ = ["RunTelemetry", "SharpnessScore", "SHARPNESS_WEIGHTS",
           "score_from_telemetry", "score_from_benchmark"]
