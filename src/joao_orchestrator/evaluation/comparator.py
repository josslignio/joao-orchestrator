"""Deterministic evaluation harness — keep-if-better comparator (C1).

Compares two integrity-signed :class:`EvaluationReport` instances (baseline vs
candidate) and emits a :class:`KeepDecision` with verdict KEEP / REJECT / TIE.

Design invariants (see repository AGENTS/invariants):

* Deterministic and provider-free: no LLM opinion participates in the verdict.
* Integrity-gated: both reports must verify their ``integrity_sha256`` and must
  match the evaluation spec hash; any mismatch raises ``ComparisonError``.
* Verdict logic:
  - candidate and baseline must both have passed;
  - a blocking regression beyond ``max_regression`` forces REJECT;
  - at least one qualifying improvement (>= ``min_improvement`` and > 0) with no
    blocking regression yields KEEP;
  - an exact tie (no improvement, no regression, both passed) follows the spec
    ``tie_policy`` (default reject);
  - otherwise REJECT.
* Persistence reuses :mod:`joao_orchestrator.storage.atomic` for durability;
  canonical (sorted, compact, ``allow_nan=False``) JSON keeps events append-only
  and reproducible.

Standard library only.
"""

from __future__ import annotations

from datetime import datetime, timezone
import math
import os
from pathlib import Path
from typing import Callable

from ..storage.atomic import atomic_write_bytes
from .models import (
    EvaluationReport,
    EvaluationSpec,
    KeepDecision,
    KeepVerdict,
    MetricDirection,
    canonical_json_bytes,
)


class ComparisonError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _metric_map(report: EvaluationReport) -> dict[str, float]:
    # Fail closed on non-finite metric values even when the report's integrity
    # hash is valid: a forged-but-integrity-valid in-memory report could carry
    # inf/nan and corrupt the verdict (Codex defect 3).
    out: dict[str, float] = {}
    for item in report.metrics:
        if not math.isfinite(item.value):
            raise ComparisonError(
                f"non-finite metric value in report: {item.name}={item.value}"
            )
        out[item.name] = item.value
    return out


class EvaluationComparator:
    def __init__(self, *, clock: Callable[[], str] = utc_now) -> None:
        self.clock = clock

    def compare(
        self,
        spec: EvaluationSpec,
        baseline: EvaluationReport,
        candidate: EvaluationReport,
    ) -> KeepDecision:
        spec.validate()
        if not baseline.verify_integrity():
            raise ComparisonError("baseline report integrity verification failed")
        if not candidate.verify_integrity():
            raise ComparisonError("candidate report integrity verification failed")
        if baseline.spec_sha256 != spec.sha256 or candidate.spec_sha256 != spec.sha256:
            raise ComparisonError("report/spec hash mismatch")

        reasons: list[str] = []
        improvements: list[str] = []
        regressions: list[str] = []
        blocking_regressions: list[str] = []

        if not candidate.passed:
            reasons.append("candidate evaluation did not pass")
        if not baseline.passed:
            reasons.append("baseline evaluation did not pass")

        baseline_metrics = _metric_map(baseline)
        candidate_metrics = _metric_map(candidate)
        expected = {rule.name for rule in spec.metrics}
        if set(baseline_metrics) != expected:
            raise ComparisonError("baseline metrics do not match evaluation spec")
        if set(candidate_metrics) != expected:
            raise ComparisonError("candidate metrics do not match evaluation spec")

        for rule in spec.metrics:
            before = baseline_metrics[rule.name]
            after = candidate_metrics[rule.name]
            if rule.direction is MetricDirection.MAXIMIZE:
                signed_change = after - before
            elif rule.direction is MetricDirection.MINIMIZE:
                signed_change = before - after
            else:
                assert rule.target is not None
                signed_change = abs(before - rule.target) - abs(after - rule.target)

            if signed_change >= rule.min_improvement and signed_change > 0:
                improvements.append(
                    f"{rule.name}: improved by {signed_change:.12g} ({before:.12g} -> {after:.12g})"
                )
            elif signed_change < 0:
                regression_amount = abs(signed_change)
                message = (
                    f"{rule.name}: regressed by {regression_amount:.12g} "
                    f"({before:.12g} -> {after:.12g})"
                )
                regressions.append(message)
                if rule.blocking and regression_amount > rule.max_regression:
                    blocking_regressions.append(message)

        if blocking_regressions:
            reasons.append("blocking metric regression detected")

        keep = (
            baseline.passed
            and candidate.passed
            and not blocking_regressions
            and bool(improvements)
        )
        if keep:
            verdict = KeepVerdict.KEEP
            reasons.append("candidate is strictly better with no blocking regression")
        elif (
            baseline.passed
            and candidate.passed
            and not blocking_regressions
            and not improvements
            and not regressions
        ):
            verdict = KeepVerdict.TIE
            if spec.tie_policy.value == "keep":
                keep = True
                reasons.append("candidate ties baseline and tie_policy=keep")
            else:
                reasons.append("candidate ties baseline and tie_policy=reject")
        else:
            verdict = KeepVerdict.REJECT
            if not reasons:
                reasons.append("candidate did not meet keep-if-better policy")

        return KeepDecision(
            verdict=verdict,
            keep=keep,
            reasons=tuple(reasons),
            improvements=tuple(improvements),
            regressions=tuple(regressions),
            blocking_regressions=tuple(blocking_regressions),
            baseline_report_sha256=baseline.integrity_sha256,
            candidate_report_sha256=candidate.integrity_sha256,
            spec_sha256=spec.sha256,
            created_at=self.clock(),
        ).with_integrity()

    def persist(self, runtime_root: Path, evaluation_id: str, decision: KeepDecision) -> None:
        # Fail closed if the decision integrity hash is missing or invalid
        # before writing it to disk (Codex defect 4).
        if not decision.verify_integrity():
            raise ComparisonError(
                "cannot persist KeepDecision with missing or invalid integrity hash"
            )
        runtime_root = runtime_root.expanduser().resolve()
        target_dir = runtime_root / "evaluations" / evaluation_id / "comparison"
        target_dir.mkdir(parents=True, exist_ok=True)
        self._atomic_write_json(target_dir / "comparison.json", decision.to_dict())
        self._atomic_write_json(target_dir / "keep_decision.json", decision.to_dict())
        events_path = runtime_root / "evaluations" / evaluation_id / "evaluation_events.jsonl"
        events_path.parent.mkdir(parents=True, exist_ok=True)
        with events_path.open("ab") as handle:
            handle.write(canonical_json_bytes({
                "event": "comparison_finished",
                "decision_sha256": decision.integrity_sha256,
                "verdict": decision.verdict.value,
                "keep": decision.keep,
                "created_at": decision.created_at,
            }))
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _atomic_write_json(path: Path, payload: object) -> None:
        # Reuse the canonical atomic-write primitive (write-tmp + fsync +
        # os.replace). Canonical bytes are supplied here so the persisted JSON
        # matches the signed integrity payload byte-for-byte.
        atomic_write_bytes(path, canonical_json_bytes(payload) + b"\n")
